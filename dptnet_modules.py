from dataclasses import dataclass
import lightning
from DPTNet.models import DPTNet_base
import torch
from dataset import MixedAudioDataLoaderOutput
from typing import Dict, Any, Callable
from torch.optim.lr_scheduler import ExponentialLR
from torchmetrics.audio.snr import ScaleInvariantSignalNoiseRatio
import tqdm
from settings import FS, infer_block_size


def rms_loudness(signal: torch.Tensor) -> torch.Tensor:
    return torch.sqrt(torch.mean(signal**2, dim=-1, keepdim=True))


def loudness_loss(
    estimated_signal: torch.Tensor, target_signal: torch.Tensor
) -> torch.Tensor:
    estimated_loudness = rms_loudness(estimated_signal)
    target_loudness = rms_loudness(target_signal)
    return (
        torch.abs(estimated_loudness - target_loudness) * 128
    )  # 100 is loudness_loss weight


def process_in_block(
    block_size: int,
    wav: torch.Tensor,
    action: Callable[[torch.Tensor], torch.Tensor],
) -> torch.Tensor:
    wav_len = wav.shape[-1]
    res = torch.zeros_like(wav)
    for i in tqdm.tqdm(range(0, wav_len, block_size)):
        source = wav[..., i : i + block_size]
        estimated_source = action(source)
        res[..., i : i + block_size] += estimated_source
    return res


@dataclass
class DPTNetModuleArgs:
    n: int = 64  # feature dim in DPT blocks
    w_ms: int = 2  # filter length in encoder in ms
    k: int = 250  # chunk size in frames
    d: int = 6  # number of DPT blocks
    h: int = 4  # number of hidden units in LSTM after multihead attention
    e: int = 256  # #channels before bottleneck
    fs: int = FS

    @property
    def w(self):
        return int(self.w_ms * self.fs // 1000)


class N2NDPTNetModule(lightning.LightningModule):
    def __init__(self, args: DPTNetModuleArgs):
        super().__init__()
        self.save_hyperparameters()
        self.args = args
        self.model = DPTNet_base(
            enc_dim=args.e,
            feature_dim=args.n,
            hidden_dim=args.h,
            layer=args.d,
            segment_size=args.k,
            win_len=args.w,
        )
        self.train_loss = torch.nn.L1Loss()
        self.eval_loss = ScaleInvariantSignalNoiseRatio()

    def _shared_step(self, batch: MixedAudioDataLoaderOutput) -> torch.Tensor:
        padded_x_hat, padded_y_hat = batch

        denoised_x_hat = self.model(padded_x_hat)
        loss = self.train_loss(denoised_x_hat, padded_y_hat)

        return loss.squeeze().mean()

    def training_step(self, batch: MixedAudioDataLoaderOutput) -> torch.Tensor:
        loss = self._shared_step(batch)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, batch: MixedAudioDataLoaderOutput) -> torch.Tensor:
        # we validate using SI-SNR against clean speech, make sure use `MixedAudioDataset` not `N2NMixedAudioDataset`
        mixed, clean = batch
        val_loss = 0 - self.eval_loss(mixed, clean).squeeze().mean()
        self.log("val_loss", val_loss)
        return val_loss

    def predict_step(self, batch: torch.Tensor) -> torch.Tensor:
        """
        Args:
            batch: T
        """

        def action(wav: torch.Tensor) -> torch.Tensor:
            with torch.no_grad():
                return self.model(wav)

        return process_in_block(infer_block_size, batch, action)

    def configure_optimizers(self) -> Dict[str, Any]:
        optimizer = torch.optim.Adam(
            self.parameters(), betas=(0.9, 0.98), eps=1e-9, lr=0.001
        )

        # Exponential decay phase
        decay_factor = 0.98**0.5  # 0.98 every two epoch
        exp_scheduler = ExponentialLR(optimizer, gamma=decay_factor)

        return {
            "optimizer": optimizer,
            "lr_scheduler": exp_scheduler,
        }
