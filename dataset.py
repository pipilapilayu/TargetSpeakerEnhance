from functools import partial
import os
import random
import torchaudio
import torch
from torch.utils.data import Dataset, DataLoader, Subset
from typing import Dict, List, Tuple, Any, Union
from glob import glob
import librosa
import librosa.display
import matplotlib.pyplot as plt
from settings import FS


Tensor = torch.Tensor


def read_wav_at_FS(filename: str) -> Tensor:
    y, fs = torchaudio.load(filename)
    # if y.shape[0] != 2:
    #     y = y.repeat(2, 1)
    if y.shape[0] > 1:
        y = y.mean(dim=0, keepdim=True)
    if fs != FS:
        y = torchaudio.functional.resample(y, fs, FS)
    return y


class CachedLoader:
    def __init__(self) -> None:
        self.map: Dict[str, Tensor] = {}

    def __getitem__(self, key: str) -> Tensor:
        if key not in self.map:
            self.map[key] = read_wav_at_FS(key)

        return self.map[key]


MixedAudioDatasetOutput = Tuple[Tensor, Tensor]


class MixedAudioDataset(Dataset):
    """
    Accept clean folder and dirty folders.
    Each time when asked to fetch something, we load, monoize, resample, and cache each file.
    Mixed files are generated by following procedure:
    0. randomly select a clean wav from files in all clean files
    1. randomly select a dirty wav from files in all dirty files
    2. randomly select a start point in dirty wav
    3. chop a segment with length = clean wav
    4. mix, but dirty file got random 0.6-1.2x amp
    5. returns mixed and clean wav
    """

    def __init__(self, clean_folder: str, dirty_folders: List[str]):
        self.clean_files = [f for f in glob(os.path.join(clean_folder, "*.wav"))]
        self.dirty_files = [
            f
            for d in dirty_folders
            for f in glob(os.path.join(d, "*.wav"))
            + glob(os.path.join(d, "*.m4a"))
            + glob(os.path.join(d, "*.mp3"))
        ]
        self.cached_loader = CachedLoader()

    def __len__(self):
        return len(self.clean_files)

    @staticmethod
    def get_max_mul(clean: Tensor, dirty: Tensor) -> float:
        # res = c + w * d, we want res in [-1, 1], so w * d in [-1 - c, 1 - c] and thus w in [(-1 - c) / d, (1 - c) / d]?
        # for each sample we calculate range, then get abs min?

        assert (
            clean.size() == dirty.size()
        ), "Clean and dirty tensors must be of the same size"

        # Mask for positive and negative dirty samples
        positive_mask = dirty > 0
        negative_mask = dirty < 0

        # Calculate lower and upper bounds
        lowerbound = (-1 - clean) / dirty
        upperbound = (1 - clean) / dirty

        # Initialize max_w with a large number
        max_w = float("inf")

        # Update max_w using the appropriate bounds depending on the sign of dirty samples
        if positive_mask.any():
            max_w = min(
                max_w,
                lowerbound[positive_mask].abs().min().item(),
                upperbound[positive_mask].abs().min().item(),
            )
        if negative_mask.any():
            max_w = min(
                max_w,
                lowerbound[negative_mask].abs().min().item(),
                upperbound[negative_mask].abs().min().item(),
            )

        return max_w

    @staticmethod
    def overlap_dirty_segment(clean_audio: Tensor, dirty_audio: Tensor) -> Tensor:
        len_clean = clean_audio.shape[-1]
        len_dirty = dirty_audio.shape[-1]

        # Randomly select a start point for the dirty audio segment
        start_point = random.randint(0, len_dirty - len_clean)
        dirty_segment = dirty_audio[:, start_point : start_point + len_clean]

        max_mul = MixedAudioDataset.get_max_mul(clean_audio, dirty_segment)
        dirty_weight = 1 - random.random() ** 2
        overlapped_audio = clean_audio + dirty_weight * max_mul * dirty_segment

        return overlapped_audio

    @staticmethod
    def apply_offset(wav: Tensor, offset: int) -> Tensor:
        # wav[0] would be located at return_wav[offset].
        if offset == 0:
            return wav
        wav_len = wav.shape[-1]
        res = torch.zeros_like(wav)
        if offset < 0:
            res[..., : wav_len + offset] = wav[..., -offset:]
        else:
            res[..., offset:] = wav[..., : wav_len - offset]
        return res

    def __getitem__(self, idx: int) -> MixedAudioDatasetOutput:
        """
        Args:
            idx: int, some random index ranging in [0..len(self))
        Returns:
            (
                mixed_wav: 1 x T, Tensor. T is number of samples.
                clean_wav: 1 x T, Tensor
            )
        """
        clean_file = self.clean_files[idx]
        clean_wav = self.cached_loader[clean_file]

        clean_wav_len = clean_wav.shape[-1]

        dirty_file = random.choice(self.dirty_files)
        dirty_wav = self.cached_loader[dirty_file]

        mixed_wav = self.overlap_dirty_segment(clean_wav, dirty_wav)

        return mixed_wav, clean_wav


class N2NMixedAudioDataset(Dataset):
    """
    Returns two corrupted (mixed) wav to support noise2noise style training.
    """

    def __init__(
        self, dataset: Union[MixedAudioDataset, Subset[MixedAudioDatasetOutput]]
    ):
        self.dataset = dataset

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, idx: int) -> MixedAudioDatasetOutput:
        """
        Args:
            idx: int, some random index ranging in [0..len(self))
        Returns:
            (
                corrupted_x: 1 x T, Tensor. T is number of samples.
                corrupted_y: 1 x T, Tensor
            )
        """
        corrupted_x, _ = self.dataset[idx]
        corrupted_y, _ = self.dataset[idx]

        return corrupted_x, corrupted_y


def pad_seq_n_stack(wavs: List[Tensor], target_len: int) -> Tensor:
    """
    Args:
        wavs: list of 1 x T Tensor, T may vary.
        target_len: assert to be max T in that varying 1 x T tensor list.
    Returns:
        result: B x target_len Tensor
    """
    padded_wavs = [
        torch.cat([wav, torch.zeros(target_len - len(wav))])
        for wav in map(lambda x: x[0], wavs)
    ]
    return torch.stack(padded_wavs)


MixedAudioDataLoaderOutput = Tuple[Tensor, Tensor]


def collate_fn(
    alignment: int, batch: List[MixedAudioDatasetOutput]
) -> MixedAudioDataLoaderOutput:
    """
    Args:
        alignment: make sure the padded size is divisible by W / 2, required by Encoder in DPTNet
        batch: list of dataset output
    Returns:
        (
            batch_padded_mixed_wav: B x T, Tensor
            batch_padded_clean_wav: B x T, Tensor
        )
    """
    pad_length = max(mixed.shape[-1] for mixed, _ in batch)
    pad_length += (alignment - pad_length % alignment) % alignment

    mixed_wavs, clean_wavs = zip(*batch)
    batch_padded_mixed_wav = pad_seq_n_stack(list(mixed_wavs), pad_length)
    batch_padded_clean_wav = pad_seq_n_stack(list(clean_wavs), pad_length)

    return (batch_padded_mixed_wav, batch_padded_clean_wav)


class MixedAudioDataLoader(DataLoader):
    def __init__(self, alignment: int, *args, **kwargs):
        super().__init__(collate_fn=partial(collate_fn, alignment), *args, **kwargs)


def plot_melspectrogram(wav, ax, fs=44100, title="Melspectrogram"):
    s = librosa.feature.melspectrogram(y=wav, sr=fs)
    librosa.display.specshow(
        librosa.power_to_db(s), x_axis="time", y_axis="mel", ax=ax, sr=fs, cmap="magma"
    )
    ax.set(title=title)


if __name__ == "__main__":
    test_dataset = MixedAudioDataset(
        "./datasets/clean/pi/bootstrap",
        [
            "./datasets/dirty/c_chan/stardew_valley",
        ],
    )

    loader = MixedAudioDataLoader(alignment=8, dataset=test_dataset, batch_size=4)
    for padded_x_hat, lengths, padded_y_hat in loader:
        fig, axes = plt.subplots(nrows=4, ncols=4, figsize=(20, 8))

        for i in range(4):
            x_hat = padded_x_hat[i].numpy()
            y_hat = padded_y_hat[i].numpy()
            dirty = x_hat - y_hat

            ax = axes[i, 0]
            plot_melspectrogram(x_hat, ax, fs=FS, title="mixed")

            ax = axes[i, 1]
            plot_melspectrogram(y_hat, ax, fs=FS, title="clean")

            ax = axes[i, 2]
            plot_melspectrogram(dirty, ax, fs=FS, title="dirty")

            ax = axes[i, 3]
            ax.plot(x_hat)

        plt.tight_layout()
        plt.show()

        break

    n2n_dataset = N2NMixedAudioDataset(test_dataset)
    n2n_loader = MixedAudioDataLoader(alignment=8, dataset=n2n_dataset, batch_size=4)
    for padded_x_hat, lengths, padded_y_hat in n2n_loader:
        fig, axes = plt.subplots(nrows=4, ncols=2, figsize=(10, 8))

        for i in range(4):
            x_hat = padded_x_hat[i].numpy()
            y_hat = padded_y_hat[i].numpy()

            ax = axes[i, 0]
            plot_melspectrogram(x_hat, ax, fs=FS, title=r"$\hat{x_i}$")

            ax = axes[i, 1]
            plot_melspectrogram(y_hat, ax, fs=FS, title=r"$\hat{y_i}$")

        plt.tight_layout()
        plt.show()

        break
