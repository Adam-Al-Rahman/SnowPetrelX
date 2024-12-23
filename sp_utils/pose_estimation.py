import os
from PIL import Image
import matplotlib.pyplot as plt

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset
from torchvision import models
from torchvision.transforms.functional import to_pil_image

class PoseDataset(Dataset):
    def __init__(self, dataframe, dataset_root_folder, img_transform=None, kp_transform=None):
        self.annotations = dataframe  # Load the pandas DataFrame directly
        self.dataset_root_folder = dataset_root_folder  # Root folder for the dataset
        self.img_transform = img_transform
        self.kp_transform = kp_transform

    def __len__(self):
        return len(self.annotations)

    def __getitem__(self, idx):
        # Construct the image path from behavior, image_id, and image_file columns
        behavior = self.annotations.iloc[idx]['behavior']
        image_id = self.annotations.iloc[idx]['image_id']
        image_file = self.annotations.iloc[idx]['image_file']

        LABELS = ["nesting", "preening"]
        
        # Create the full image path
        img_path = os.path.join(self.dataset_root_folder, LABELS[behavior], image_id, image_file)
        
        # Load and process the image
        image = Image.open(img_path).convert("RGB")
        
        # Extract the keypoints (head_x, head_y, ..., body2_x, body2_y) as numpy array
        keypoints = self.annotations.iloc[idx, 3:].values.astype('float32')
        
        if self.img_transform:
            image = self.img_transform(image)

        if self.kp_transform:
            keypoints = self.kp_transform(keypoints)
        

        return image, keypoints



class NormalizeKeypoints:
    def __init__(self, image_width: int, image_height: int):
        self.image_width = image_width
        self.image_height = image_height

    def __call__(self, keypoints):
        # Ensure keypoints is a tensor and of correct dtype
        if not isinstance(keypoints, torch.Tensor):
            keypoints = torch.tensor(keypoints, dtype=torch.float32)
        else:
            keypoints = keypoints.clone().float()  # Avoid modifying the original tensor
        
        # Normalize x and y coordinates
        keypoints[0::2] /= self.image_width
        keypoints[1::2] /= self.image_height
        return keypoints


class DenormalizeKeypoints:
    def __init__(self, image_width: int, image_height: int):
        self.image_width = image_width
        self.image_height = image_height

    def __call__(self, keypoints):
        # Ensure keypoints is a tensor and of correct dtype
        if not isinstance(keypoints, torch.Tensor):
            keypoints = torch.tensor(keypoints, dtype=torch.float32)
        else:
            keypoints = keypoints.clone().float()  # Avoid modifying the original tensor
        
        # Denormalize x and y coordinates
        keypoints[0::2] *= self.image_width
        keypoints[1::2] *= self.image_height
        return keypoints


def calculate_head_size(keypoints):
    """
    Calculate head size for a batch of flattened keypoints.

    Args:
        keypoints (torch.Tensor): A tensor of shape (batch_size, num_keypoints * 2),
                                  where each row contains flattened 2D coordinates.
                                  Keypoints are arranged as:
    Returns:
        torch.Tensor: A tensor of shape (batch_size,) containing the head size for each sample.
    """
    # Extract batch size and number of keypoints
    batch_size = keypoints.size(0)  # First dimension is the batch size
    num_keypoints = keypoints.size(1) // 2  # Number of keypoints

    # Reshape to (batch_size, num_keypoints, 2)
    keypoints = keypoints.view(batch_size, num_keypoints, 2)

    # Extract head and beak_tip keypoints
    head = keypoints[:, 0, :]       # Shape: (batch_size, 2)
    beak_tip = keypoints[:, 2, :]   # Shape: (batch_size, 2)

    # Calculate Euclidean distance between head and beak_tip
    head_size = torch.norm(head - beak_tip, p=2, dim=1)  # Shape: (batch_size,)

    return head_size


# TODO(Adam-Al-Rahman): Remove the comments after optimizing the training and testing workflow
# When the threshold is 0.2 in the PCKh (Percentage of Correct Keypoints with Head Normalization) calculation,
# it means that a predicted keypoint is considered correct
# if the Euclidean distance between the predicted and ground truth keypoints is less than 20% of the head size.

def pckh(predictions, ground_truth, head_size, threshold=0.2):
    """
    Calculate PCKh (Percentage of Correct Keypoints with Head Normalization) for a batch of predictions.

    Args:
        predictions (Tensor): Predicted keypoints, shape (batch_size, num_keypoints * 2)
        
        ground_truth (Tensor): Ground truth keypoints, shape (batch_size, num_keypoints * 2)
        head_size (Tensor): Normalizing head size for each sample, shape (batch_size,)
        threshold (float): Normalized distance threshold (percentage of head size)

    Returns:
        float: PCKh metric as a percentage of correct keypoints
    """
    batch_size, num_flattened = predictions.size()
    num_keypoints = num_flattened // 2  # Derive number of keypoints
    
    # Reshape flattened predictions and ground truth to (batch_size, num_keypoints, 2)
    predictions = predictions.view(batch_size, num_keypoints, 2)
    ground_truth = ground_truth.view(batch_size, num_keypoints, 2)
    
    # Calculate Euclidean distance between predicted and ground truth keypoints
    distance = torch.norm(predictions - ground_truth, p=2, dim=2)  # shape: (batch_size, num_keypoints)
    
    # Normalize by head size for PCKh
    normalized_distance = distance / head_size.unsqueeze(1)  # shape: (batch_size, num_keypoints)
    
    # Calculate PCKh: Count keypoints that are within the threshold
    correct_keypoints = (normalized_distance < threshold).float()  # shape: (batch_size, num_keypoints)
    
    # Compute the percentage of correct keypoints
    pckh = correct_keypoints.sum() / (batch_size * num_keypoints) * 100
    
    return pckh.item()

# TODO(Adam-Al-Rahman): remove after testing phase
# The Average PCKh (Percentage of Correct Keypoints with Head Normalization) being 94% means that on average,
# only 94% of the predicted keypoints are within the specified "threshold=0.2" (e.g., 20% of the head size) across the dataset.

def pe_accuracy(model, dataloader, device, threshold=0.2):
    """
    Calculate PCKh accuracy for the entire dataset.

    Args:
        model (nn.Module): The pose estimation model
        dataloader (DataLoader): DataLoader providing the dataset
        device (str): Device to run the model on (either 'cuda' or 'cpu')

    Returns:
        float: Average PCKh for the dataset
    """
    model.eval()  # Set model to evaluation mode
    total_pckh = 0.0
    total_samples = 0

    with torch.inference_mode():  # Disable gradient calculation for evaluation
        for images, keypoints in dataloader:
            images = images.to(device)
            keypoints = keypoints.to(device)
            head_sizes = calculate_head_size(keypoints).to(device)
            
            # Predict keypoints
            outputs = model(images)
            
            # Calculate PCKh for the current batch
            batch_pckh = pckh(outputs, keypoints, head_sizes, threshold)
            
            total_pckh += batch_pckh * images.size(0)
            total_samples += images.size(0)

    # Calculate the average PCKh for the dataset
    average_pckh = total_pckh / total_samples
    return average_pckh


def plot(model, data_loader, device, kp_denormalize, plot_num_batches=1):
    """
    Plot predictions and ground truth keypoints for images from a data loader.
    
    Args:
        model: Trained PyTorch model for pose estimation.
        data_loader: DataLoader for the dataset (e.g., test_loader).
        device: Device to perform computations on ('cpu' or 'cuda').
        kp_denormalize: An instance of DenormalizeKeypoints for denormalizing keypoints.
        plot_num_batches: Number of batches to visualize.
    """
    model.eval()  # Set model to evaluation mode
    
    # Iterate through the loader for a few batches
    batch_count = 0
    with torch.inference_mode():
        for images, ground_truth_kps in data_loader:
            if batch_count >= plot_num_batches:
                break
            
            images = images.to(device)

            # Convert ground truth keypoints to numpy and denormalize
            ground_truth_kps = ground_truth_kps.cpu().numpy()
            ground_truth_kps = np.array([
                kp_denormalize(keypoints).numpy() for keypoints in ground_truth_kps
            ])
            ground_truth_kps = ground_truth_kps.reshape(-1, ground_truth_kps.shape[1] // 2, 2)  # Reshape to (B, N, 2)

            # Get predictions, denormalize, and reshape
            predictions = model(images).cpu().numpy()
            predictions = np.array([
                kp_denormalize(keypoints).numpy() for keypoints in predictions
            ])
            predictions = predictions.reshape(-1, ground_truth_kps.shape[1], 2)  # Reshape to (B, N, 2)

            # Visualize each image in the batch
            for i in range(len(images)):
                pil_image = to_pil_image(images[i].cpu())
                
                # Plot the image
                plt.figure(figsize=(8, 8))
                plt.imshow(pil_image)
                plt.axis('off')
                
                # Plot ground truth keypoints
                plt.scatter(
                    ground_truth_kps[i][:, 0], 
                    ground_truth_kps[i][:, 1], 
                    c='green', label='Expected', s=40, marker='o'
                )
                
                # Plot predicted keypoints
                plt.scatter(
                    predictions[i][:, 0], 
                    predictions[i][:, 1], 
                    c='red', label='Prediction', s=40, marker='x'
                )

                # Add legend and title
                plt.legend()
                plt.title(f"Prediction vs Expected (Batch {batch_count + 1}, Image {i + 1})")
                plt.show()
            
            batch_count += 1


def result(model, data_loader, device, kp_denormalize):
    model.eval()  # Set model to evaluation mode
    all_predictions = []  # List to accumulate predictions for all batches

    with torch.inference_mode():
        for images, ground_truth_kps in data_loader:
            images = images.to(device)

            # Convert ground truth keypoints to numpy and denormalize
            ground_truth_kps = kp_denormalize(ground_truth_kps).cpu().numpy()
            ground_truth_kps = ground_truth_kps.reshape(-1, ground_truth_kps.shape[1] // 2, 2)  # Reshape to (B, N, 2)

            # Get predictions and denormalize
            predictions = model(images).cpu().numpy()
            predictions = kp_denormalize(torch.tensor(predictions)).cpu().numpy()

            # Append batch predictions to the results list
            all_predictions.append(predictions)

    # Convert the list of numpy arrays to a single numpy array, then to a tensor
    all_predictions = np.concatenate(all_predictions, axis=0)
    return torch.tensor(all_predictions)



# MODEL Architecture

class BirdPoseModel(nn.Module):
    def __init__(self, num_keypoints: int, name = "resnet50_relu"): # "resnet50_batch_norm2d_relu"
        super(BirdPoseModel, self).__init__()
        self.name = name
        
        # Load ResNet-50 backbone and remove the last two layers
        resnet = models.resnet50(weights=models.ResNet50_Weights.DEFAULT)
        self.backbone = nn.Sequential(*list(resnet.children())[:-2])
        
        # Reduce the channel size progressively
        self.conv_layers = nn.Sequential(
            nn.Conv2d(2048, 1024, kernel_size=3, padding=1),
            nn.BatchNorm2d(1024),
            nn.ReLU(),
            nn.Conv2d(1024, 512, kernel_size=3, padding=1),
            nn.BatchNorm2d(512),
            nn.ReLU(),
            nn.Conv2d(512, 256, kernel_size=3, padding=1),
            nn.BatchNorm2d(256),
            nn.ReLU(),
            nn.Conv2d(256, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU()
        )
        
        # Global average pooling
        self.global_avg_pool = nn.AdaptiveAvgPool2d(1)
        
        # Fully connected layer for keypoints
        self.fc = nn.Linear(64, num_keypoints * 2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Pass input through the ResNet backbone
        x = self.backbone(x)
        
        # Apply the convolutional layers
        x = self.conv_layers(x)
        
        # Global average pooling
        x = self.global_avg_pool(x)  # Shape: (batch_size, 64, 1, 1)
        x = torch.flatten(x, 1)  # Shape: (batch_size, 64)
        
        # Fully connected layer for keypoint prediction
        x = self.fc(x)  # Shape: (batch_size, num_keypoints * 2)
        
        return x


## MODEL - II Architecture

class BirdPoseModelX(nn.Module):
    def __init__(self, num_keypoints: int, name="resnet50_batch_norm2d_swish_dropout"):
        super(BirdPoseModelX, self).__init__()
        self.name = name
        
        # Load ResNet-50 backbone and remove the last two layers
        resnet = models.resnet50(weights=models.ResNet50_Weights.DEFAULT)
        self.backbone = nn.Sequential(*list(resnet.children())[:-2])
        
        # Reduce the channel size progressively with BatchNorm, SiLU, and Dropout
        self.conv_layers = nn.Sequential(
            nn.Conv2d(2048, 1024, kernel_size=3, padding=1),
            nn.BatchNorm2d(1024),
            nn.SiLU(),
            nn.Dropout(p=0.3),
            
            nn.Conv2d(1024, 512, kernel_size=3, padding=1),
            nn.BatchNorm2d(512),
            nn.SiLU(),
            nn.Dropout(p=0.3),
            
            nn.Conv2d(512, 256, kernel_size=3, padding=1),
            nn.BatchNorm2d(256),
            nn.SiLU(),
            nn.Dropout(p=0.3),
            
            nn.Conv2d(256, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.SiLU(),
            nn.Dropout(p=0.3)
        )
        
        # Global average pooling
        self.global_avg_pool = nn.AdaptiveAvgPool2d(1)
        
        # Fully connected layer for keypoints
        self.fc = nn.Linear(64, num_keypoints * 2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Pass input through the ResNet backbone
        x = self.backbone(x)
        
        # Apply the convolutional layers
        x = self.conv_layers(x)
        
        # Global average pooling
        x = self.global_avg_pool(x)  # Shape: (batch_size, 64, 1, 1)
        x = torch.flatten(x, 1)  # Shape: (batch_size, 64)
        
        # Fully connected layer for keypoint prediction
        x = self.fc(x)  # Shape: (batch_size, num_keypoints * 2)
        
        return x
