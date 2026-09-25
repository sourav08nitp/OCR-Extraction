"""Batched pix2tex inference.

pix2tex's LatexOCR.__call__ handles one image at a time, which leaves a GPU mostly idle: the decoder
emits one token per step. Here the preprocessing is the same as __call__, but images whose final
tensor shape matches are decoded together in one batch.
"""

from collections import defaultdict

import numpy as np
import torch
from PIL import Image
from pix2tex.cli import minmax_size
from pix2tex.dataset.transforms import test_transform
from pix2tex.utils import pad, post_process, token2str


def _prepare(model, img):
    """Same steps as LatexOCR.__call__ up to the model input tensor (1, 1, H, W)."""
    args = model.args
    img = minmax_size(pad(img), args.max_dimensions, args.min_dimensions)
    if model.image_resizer is not None and not args.no_resize:
        with torch.no_grad():
            input_image = img.convert("RGB").copy()
            r, w, h = 1, input_image.size[0], input_image.size[1]
            for _ in range(10):
                h = int(h * r)
                img = pad(minmax_size(
                    input_image.resize((w, h), Image.Resampling.BILINEAR if r > 1 else Image.Resampling.LANCZOS),
                    args.max_dimensions, args.min_dimensions))
                t = test_transform(image=np.array(img.convert("RGB")))["image"][:1].unsqueeze(0)
                w = (model.image_resizer(t.to(args.device)).argmax(-1).item() + 1) * 32
                if w == img.size[0]:
                    break
                r = w / img.size[0]
    else:
        t = test_transform(image=np.array(pad(img).convert("RGB")))["image"][:1].unsqueeze(0)
    return t


def _decode(model, rows):
    eos = model.args.eos_token
    out = []
    for row in rows:
        hits = (row == eos).nonzero()
        # in a batch, rows that finished early keep sampling until every row hits EOS
        if len(hits):
            row = row[: hits[0].item()]
        out.append(post_process(token2str(row, model.tokenizer)[0]))
    return out


def predict_batch(model, images, batch_size=16, progress=None):
    """images: list of PIL images -> list of LaTeX strings (None where prediction failed)."""
    results = [None] * len(images)
    # only identical shapes share a batch: padding images to a common width measurably changes the output
    groups = defaultdict(list)
    for i, img in enumerate(images):
        try:
            t = _prepare(model, img)
            groups[tuple(t.shape)].append((i, t))
        except Exception:
            pass
        if progress:
            progress("preparing formulas", i + 1, len(images))

    done = 0
    temperature = model.args.get("temperature", 0.25)
    for items in groups.values():
        for k in range(0, len(items), batch_size):
            chunk = items[k:k + batch_size]
            x = torch.cat([t for _, t in chunk]).to(model.args.device)
            try:
                with torch.no_grad():
                    dec = model.model.generate(x, temperature=temperature)
                for (i, _), tex in zip(chunk, _decode(model, dec.cpu())):
                    results[i] = tex
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                for i, t in chunk:
                    results[i] = _single(model, t)
            done += len(chunk)
            if progress:
                progress("latex", done, len(images))
    return results


def _single(model, t):
    try:
        with torch.no_grad():
            dec = model.model.generate(t.to(model.args.device), temperature=model.args.get("temperature", 0.25))
        return _decode(model, dec.cpu())[0]
    except Exception:
        return None
