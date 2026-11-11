"""Cut the approved MateBot main graphic (P8 / P8(a)) out of its cream background for the landing and done screens.

Source: brand/final_delivery_20261105/MateBot_Final_Mascot.png (pixel-faithful crop of the approved master
brand/approved/MateBot_and_HomeBot_Approved_Master_20261104.png). Nothing in the artwork is redrawn: only an alpha
channel is added. The body "white" in the master is the same cream as the background, so the matte is built from the
dark outlines (closed silhouette) rather than from colour alone; true gaps between limbs and the hole in the locator
pin stay transparent. Output: client/img/matebot_landing.webp, matebot_main.webp, matebot_main.png.
"""
from pathlib import Path
import numpy as np, cv2
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]          # .../files
SRC = ROOT / 'brand/final_delivery_20261105/MateBot_Final_Mascot.png'
OUT = ROOT / 'mmai-app/client/img'
BG = np.array([251.4, 250.4, 233.7])                 # measured cream of the master background
HOLE_MAX = 3000                                      # px: enclosed cream areas smaller than this stay see-through
CLOSE = 7                                            # px: closes small breaks in the outlines so the body stays solid


def main():
    rgb = np.array(Image.open(SRC).convert('RGB')).astype(float)
    H, W = rgb.shape[:2]
    d = np.abs(rgb - BG).sum(-1)
    ink = (d >= 24).astype(np.uint8)                 # anything that is clearly not background cream
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * CLOSE + 1, 2 * CLOSE + 1))
    closed = cv2.dilate(ink, k)
    # Fill enclosed regions by flooding from the border. Large enclosed cream areas are the body, helmet ring, window
    # frame and window opening and stay solid; small enclosed ones (letter counters, the locator pin's hole, slivers
    # between limbs) are real see-through gaps and stay transparent.
    inv = (1 - closed).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    solid = closed.copy()
    for i in range(1, n):
        if i in border:
            continue
        if st[i, 4] >= HOLE_MAX:
            solid[lab == i] = 1
    solid = cv2.erode(solid, k)                      # undo the dilation
    core = cv2.erode(solid, np.ones((3, 3), np.uint8))
    near = cv2.dilate(solid, np.ones((5, 5), np.uint8))
    # alpha: 1 in the core; the outline's anti-aliasing against cream on the rim; nothing further out (no painterly haze)
    edge = np.clip((d - 14) / 36.0, 0, 1)
    alpha = np.where(core == 1, 1.0, np.where(near == 1, edge, 0.0))
    # un-premultiply the fringe so the outline colour does not carry a cream halo onto a different page colour
    a = alpha[..., None]
    col = np.where((a > 0.02) & (a < 0.98), np.clip((rgb - (1 - a) * BG) / np.maximum(a, 1e-3), 0, 255), rgb)
    out = np.dstack([col, alpha * 255]).round().astype(np.uint8)
    img = Image.fromarray(out, 'RGBA')
    # trim transparent margins, keep a small breathing space
    bbox = img.getbbox(); pad = 10
    img = img.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(W, bbox[2] + pad), min(H, bbox[3] + pad)))
    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / 'matebot_landing.webp', 'WEBP', quality=86, method=6)
    img.save(OUT / 'matebot_main.webp', 'WEBP', quality=86, method=6)
    img.save(OUT / 'matebot_main.png', 'PNG', optimize=True)
    img.save('/tmp/matebot_cutout_preview.png')
    print('size', img.size, 'alpha mean %.3f' % (out[..., 3].mean() / 255))


if __name__ == '__main__':
    main()
