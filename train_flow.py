import argparse
import os

import mlflow
import torch
from torch.optim import *

from configs.parser import YAMLParser
from dataloader.h5 import H5Loader
from loss.flow import EventWarping
from models.model import (
    FireNet,
    FireNet_short,
    RNNFireNet,
    LeakyFireNet,
    FireFlowNet,
    LeakyFireFlowNet,
    E2VID,
    EVFlowNet,
    RecEVFlowNet,
    LeakyRecEVFlowNet,
    RNNRecEVFlowNet,
)
from models.model import (
    LIFFireNet,
    LIFFireNet_short,
    PLIFFireNet,
    ALIFFireNet,
    XLIFFireNet,
    LIFFireFlowNet,
    SpikingRecEVFlowNet,
    PLIFRecEVFlowNet,
    ALIFRecEVFlowNet,
    XLIFRecEVFlowNet,
)
from utils.gradients import get_grads
from utils.utils import load_model, save_csv, save_diff, save_model
from utils.visualization import Visualization

def get_next_model_folder(base_path="mlruns/0/models/"):
    index = 0
    while os.path.exists(os.path.join(base_path, str(index))):
        index += 1
    return os.path.join(base_path, str(index))


def calibrate_quantization(model, dataloader, config, device, num_samples=100):
    """Calibrate quantization parameters by running inference on sample data."""
    print(f"Starting quantization calibration with {num_samples} samples...")
    
    model.eval()
    model.enable_quantization_calibration()
    
    sample_count = 0
    with torch.no_grad():
        for inputs in dataloader:
            if sample_count >= num_samples:
                break
                
            # Forward pass for calibration
            _ = model(inputs["event_voxel"].to(device), inputs["event_cnt"].to(device))
            sample_count += config["loader"]["batch_size"]
            
            if sample_count % 10 == 0:
                print(f"Calibration progress: {sample_count}/{num_samples}")
    
    model.disable_quantization_calibration()
    model.train()
    print("Quantization calibration completed.")


def train(args, config_parser):
    mlflow.set_tracking_uri(args.path_mlflow)

    # configs
    config = config_parser.config
    if config["data"]["mode"] == "frames":
        print("Config error: Training pipeline not compatible with frames mode.")
        raise AttributeError

    # log config
    mlflow.set_experiment(config["experiment"])
    mlflow.start_run()
    mlflow.log_params(config)
    mlflow.log_param("prev_runid", args.prev_runid)
    config = config_parser.combine_entries(config)
    mlflow.pytorch.autolog()
    print("MLflow dir:", mlflow.active_run().info.artifact_uri[:-9])

    # log git diff
    save_diff("train_diff.txt")

    # initialize settings
    device = config_parser.device
    kwargs = config_parser.loader_kwargs

    # visualization tool
    if config["vis"]["enabled"]:
        vis = Visualization(config)

    # data loader
    data = H5Loader(config, config["model"]["num_bins"], config["model"]["round_encoding"])
    dataloader = torch.utils.data.DataLoader(
        data,
        drop_last=True,
        batch_size=config["loader"]["batch_size"],
        collate_fn=data.custom_collate,
        worker_init_fn=config_parser.worker_init_fn,
        **kwargs,
    )

    # loss function
    loss_function = EventWarping(config, device)

    # model initialization and settings
    model = eval(config["model"]["name"])(config["model"].copy()).to(device)
    model = load_model(args.prev_runid, model, device)
    
    # Log quantization info
    if hasattr(model, 'quant_config') and model.quant_config.use_quantization:
        print(f"Using quantization: {model.quant_config.data_type}")
        print(f"Activation bits: {model.quant_config.activation_bits}")
        print(f"Weight bits: {model.quant_config.weight_bits}")
        print(f"State bits: {model.quant_config.state_bits}")
        
        # Perform calibration if needed (only for inference, skip during training)
        if args.calibrate_only:
            calibrate_quantization(
                model, dataloader, config, device, 
                config.get("quantization", {}).get("calibration_samples", 100)
            )
            print("Calibration completed. Exiting.")
            return
    else:
        print("Using FP32 precision")
    
    model.train()

    # optimizers
    optimizer = eval(config["optimizer"]["name"])(model.parameters(), lr=config["optimizer"]["lr"])
    optimizer.zero_grad()

    # simulation variables
    patience = 50
    epochs_without_improvement = 0
    train_loss = 0
    best_loss = 1.0e6
    end_train = False
    grads_w = []

    # training loop
    data.shuffle()
    while True:
        for inputs in dataloader:

            if data.new_seq:
                data.new_seq = False

                loss_function.reset()
                model.reset_states()
                optimizer.zero_grad()

            if data.seq_num >= len(data.files):
                avg_train_loss = train_loss / (data.samples + 1)
                mlflow.log_metric("loss", avg_train_loss, step=data.epoch)

                with torch.no_grad():
                    if avg_train_loss < best_loss - 1e-6:  # small delta to prevent stopping on tiny changes
                        model_save_path = get_next_model_folder("mlruns/0/models/LIFFireNet_short_16ch/") # model: LIFFireNet             SAVING PATH ---------------------------------------------- 
                        #model_save_path = get_next_model_folder("mlruns/0/models/LIFEVFlowNet/") # model: SpikingRecEVFlowNet
                        #model_save_path = get_next_model_folder("mlruns/0/models/test/")
                        mlflow.pytorch.save_model(model, model_save_path)
                        best_loss = avg_train_loss
                        epochs_without_improvement = 0
                    else:
                        epochs_without_improvement += 1

                data.epoch += 1
                data.samples = 0
                train_loss = 0
                data.seq_num = data.seq_num % len(data.files)

                # save grads to file
                if config["vis"]["store_grads"]:
                    save_csv(grads_w, "grads_w.csv")
                    grads_w = []

                # finish training loop
                if data.epoch == config["loader"]["n_epochs"] or epochs_without_improvement >= patience:
                    print(f"Stopping at epoch {data.epoch}.")
                    end_train = True

            # forward pass
            x = model(inputs["event_voxel"].to(device), inputs["event_cnt"].to(device))

            # event flow association
            loss_function.event_flow_association(
                x["flow"],
                inputs["event_list"].to(device),
                inputs["event_list_pol_mask"].to(device),
                inputs["event_mask"].to(device),
            )

            # backward pass
            if loss_function.num_events >= config["data"]["window_loss"]:

                # overwrite intermediate flow estimates with the final ones
                if config["loss"]["overwrite_intermediate"]:
                    loss_function.overwrite_intermediate_flow(x["flow"])

                # loss
                loss = loss_function()
                train_loss += loss.item()

                # update number of loss samples seen by the network
                data.samples += config["loader"]["batch_size"]

                loss.backward()

                # clip and save grads
                if config["loss"]["clip_grad"] is not None:
                    torch.nn.utils.clip_grad.clip_grad_norm_(model.parameters(), config["loss"]["clip_grad"])
                if config["vis"]["store_grads"]:
                    grads_w.append(get_grads(model.named_parameters()))

                optimizer.step()
                optimizer.zero_grad()

                # mask flow for visualization
                flow_vis = x["flow"][-1].clone()
                if model.mask and config["vis"]["enabled"] and config["loader"]["batch_size"] == 1:
                    flow_vis *= loss_function.event_mask

                model.detach_states()
                loss_function.reset()

                # visualize
                with torch.no_grad():
                    if config["vis"]["enabled"] and config["loader"]["batch_size"] == 1:
                        vis.update(inputs, flow_vis, None)

            # print training info
            if config["vis"]["verbose"]:
                print(
                    "Train Epoch: {:04d} [{:03d}/{:03d} ({:03d}%)] Loss: {:.6f}".format(
                        data.epoch,
                        data.seq_num,
                        len(data.files),
                        int(100 * data.seq_num / len(data.files)),
                        train_loss / (data.samples + 1),
                    ),
                    end="\r",
                )

        if end_train:
            break

    mlflow.end_run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="configs/train_flow.yml",
        help="training configuration",
    )
    parser.add_argument(
        "--path_mlflow",
        default="",
        help="location of the mlflow ui",
    )
    parser.add_argument(
        "--prev_runid",
        default="",
        help="pre-trained model to use as starting point",
    )
    parser.add_argument(
        "--calibrate_only",
        action="store_true",
        help="only perform quantization calibration and exit",
    )
    args = parser.parse_args()

    # launch training
    train(args, YAMLParser(args.config))
