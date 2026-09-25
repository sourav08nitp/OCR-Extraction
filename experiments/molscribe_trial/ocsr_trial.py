import os, sys, time, json, warnings
import multiprocessing


class _SerialPool:
    """MolScribe opens Pool(16) for post-processing. On Windows every worker re-imports this script and
    loads its own copy of the model, which exhausted RAM. Run the same work one item at a time instead."""
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def map(self, f, it, chunksize=None): return [f(x) for x in it]
    def starmap(self, f, it, chunksize=None): return [f(*x) for x in it]
    def imap(self, f, it, chunksize=1): return (f(x) for x in it)
    def close(self): pass
    def join(self): pass


multiprocessing.Pool = _SerialPool
if __name__ != "__main__":  # belt and braces: never run the trial inside a helper process
    sys.exit(0)
warnings.filterwarnings("ignore")
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
from pathlib import Path
from rdkit import Chem, RDLogger
from rdkit.Chem import Draw
from PIL import Image, ImageDraw, ImageFont
RDLogger.DisableLog("rdApp.*")

import ctypes, threading


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def free_mb():
    s = _MemStatus(); s.dwLength = ctypes.sizeof(s)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.ullAvailPhys // 2**20


LOW = [free_mb()]


def _watchdog():  # the laptop froze from low memory earlier: stop ourselves before that can happen
    while True:
        f = free_mb(); LOW[0] = min(LOW[0], f)
        if f < 350:
            print(f"\nSTOPPED: free RAM fell to {f} MB", flush=True)
            os._exit(3)
        time.sleep(0.5)


print(f"free RAM at start: {free_mb()} MB", flush=True)
threading.Thread(target=_watchdog, daemon=True).start()

S = Path(__file__).parent
IMG = S / "struct"
TESTS = [  # (file, what it is, correct SMILES)
    ("p01_s04.png", "spiro ketal (Q2 option 1)", "CC1(C)COC2(CCCCC2)OC1"),
    ("p01_s10.png", "pyrrolidine", "C1CCNC1"),
    ("p02_s05.png", "m-toluic acid", "Cc1cccc(C(=O)O)c1"),
    ("p02_s06.png", "phenylacetic acid", "OC(=O)Cc1ccccc1"),
    ("p02_s08.png", "furfural (lone-pair dots)", "O=Cc1ccco1"),
    ("p13_s01.png", "acetyl chloride (condensed)", "CC(Cl)=O"),
    ("p13_s02.png", "diacetamide (condensed)", "CC(=O)NC(C)=O"),
    ("p13_s03.png", "acetic anhydride (condensed)", "CC(=O)OC(C)=O"),
    ("p13_s06.png", "ethyl carbamate (condensed)", "CCOC(N)=O"),
    ("p13_s07.png", "methyl formate (condensed)", "COC=O"),
    ("p13_s08.png", "phosgene (condensed)", "O=C(Cl)Cl"),
    ("p13_s09.png", "ethyl pivalate (condensed)", "CCOC(=O)C(C)(C)C"),
    ("p03_s10.png", "C6H5CH(CH2NO2)2 (messy crop)", "O=[N+]([O-])CC(C[N+](=O)[O-])c1ccccc1"),
]


def canon(smi):
    try:
        m = Chem.MolFromSmiles(smi) if smi else None
        return Chem.MolToSmiles(m) if m else None
    except Exception:
        return None


results = {name: {} for name, _, _ in TESTS}

# ---- MolScribe
import torch
from huggingface_hub import hf_hub_download
from molscribe import MolScribe
t = time.time()
ms = MolScribe(r"D:\claudeee\Boards_Extractor\models\molscribe_slim.pth", device=torch.device("cpu"))
print(f"MolScribe loaded in {time.time()-t:.0f}s", flush=True)
t = time.time()
for name, _, _ in TESTS:
    try:
        o = ms.predict_image_file(str(IMG / name), return_confidence=True)
        results[name]["molscribe"] = (o.get("smiles"), round(float(o.get("confidence", 0)), 2))
    except Exception as e:
        results[name]["molscribe"] = (None, f"error {type(e).__name__}")
print(f"MolScribe: {(time.time()-t)/len(TESTS):.2f}s per image", flush=True)

# ---- DECIMER (its model is only hosted on zenodo.org, which this network cannot reach)
predict_SMILES = None
print("DECIMER skipped: model host zenodo.org unreachable from this network", flush=True)
t = time.time()
for name, _, _ in TESTS:
    try:
        results[name]["decimer"] = (predict_SMILES(str(IMG / name)) if predict_SMILES else None, None)
    except Exception as e:
        results[name]["decimer"] = (None, f"error {type(e).__name__}")

# ---- score + comparison sheet
score = {"molscribe": 0, "decimer": 0}
rows = []
for name, what, truth in TESTS:
    ct = canon(truth)
    row = {"file": name, "what": what, "truth": truth}
    for tool in ("molscribe", "decimer"):
        smi, extra = results[name][tool]
        ok = canon(smi) == ct and ct is not None
        score[tool] += ok
        row[tool] = {"smiles": smi, "extra": extra, "correct": ok}
    rows.append(row)
    print(f"{name:13} {what:32} MolScribe {'OK ' if row['molscribe']['correct'] else 'X  '} {str(row['molscribe']['smiles'])[:45]:45} "
          f"DECIMER {'OK ' if row['decimer']['correct'] else 'X  '} {str(row['decimer']['smiles'])[:45]}")
print(f"\nSCORE  MolScribe {score['molscribe']}/{len(TESTS)}   (DECIMER skipped)")
print(f"lowest free RAM during run: {LOW[0]} MB")
(S / "ocsr_results.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")

W, H = 260, 170
sheet = Image.new("RGB", (W * 3, (H + 34) * len(TESTS) + 30), "white")
d = ImageDraw.Draw(sheet)
for col, title in enumerate(["Original crop", "MolScribe (redrawn)", "DECIMER (redrawn)"]):
    d.text((col * W + 8, 8), title, fill="black")
for i, row in enumerate(rows):
    y = 30 + i * (H + 34)
    im = Image.open(IMG / row["file"]).convert("RGB"); im.thumbnail((W - 16, H - 10))
    sheet.paste(im, (8, y))
    d.text((8, y + H), row["what"], fill="black")
    for col, tool in ((1, "molscribe"), (2, "decimer")):
        smi = row[tool]["smiles"]
        m = Chem.MolFromSmiles(smi) if smi else None
        if m:
            sheet.paste(Draw.MolToImage(m, size=(W - 16, H - 10)), (col * W + 8, y))
        else:
            d.text((col * W + 8, y + 60), "no valid molecule", fill="red")
        d.text((col * W + 8, y + H), ("CORRECT  " if row[tool]["correct"] else "WRONG  ") + str(smi)[:34],
               fill="green" if row[tool]["correct"] else "red")
    d.line([(0, y + H + 30), (W * 3, y + H + 30)], fill="#ddd")
sheet.save(S / "ocsr_compare.png")
print("sheet saved")
