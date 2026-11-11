"""Bring the 29 MateBot question poses up to the approved P8 design (P8(a), I20(c)).

Sources (unchanged): brand/poses/matebot_pose_<pose>.png, 1024x1536 RGBA, the original pose set already in MateBot's
original white / dark plum / warm orange colours (P5(d)). Only two design details differ from the approved master:
  * the tummy lettering "AI" becomes "SI"  - the S is the approved master's own S (shape taken from
    brand/final_delivery_20261105/MateBot_Final_Mascot.png), recoloured to the pose's letter colour, the pose's own I
    is kept and the pair is re-centred where "AI" was, following the pose's lettering tilt;
  * the round red antenna ball becomes the approved red hollow locator pin (P5(e)) - the master's pin pixels, scaled,
    with its pointed tip joined to the top of the existing spring.
Everything else in each pose (expression, limbs, props, outline, colours) is untouched. The key pose loses its golden
key (P5: no key in his hand); the raised fist is kept. Outputs: client/img/matebot_<pose>.webp and .png at 360x540, as
before. A contact sheet is written to /tmp/matebot_poses_sheet.png for review.
"""
from pathlib import Path
import json, math, sys
import numpy as np, cv2
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'brand/poses'
MASTER = ROOT / 'brand/final_delivery_20261105/MateBot_Final_Mascot.png'
OUT = ROOT / 'mmai-app/client/img'
PIN_SCALE = 0.82          # pin width as a fraction of the old ball's width ("pronounced" locator, P5(e))
OUT_W, OUT_H = 360, 540


def comps(mask, min_area=1):
    n, lab, st, cen = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
    return [dict(i=i, x=int(st[i, 0]), y=int(st[i, 1]), w=int(st[i, 2]), h=int(st[i, 3]), area=int(st[i, 4]),
                 cx=float(cen[i, 0]), cy=float(cen[i, 1])) for i in range(1, n) if st[i, 4] >= min_area], lab


def angle_of(mask):
    """Tilt (degrees, clockwise positive in image space) of a tall letter from its principal axis."""
    ys, xs = np.nonzero(mask)
    pts = np.column_stack([xs, ys]).astype(np.float32)
    mean = pts.mean(0); cov = np.cov((pts - mean).T)
    vals, vecs = np.linalg.eigh(cov); v = vecs[:, np.argmax(vals)]
    if v[1] < 0: v = -v
    return math.degrees(math.atan2(v[0], v[1]))   # 0 = upright, +ve leans top-to-the-right


def master_parts():
    """S glyph (alpha only) and the red hollow pin (RGBA) from the approved master, both de-rotated to upright."""
    rgb = np.array(Image.open(MASTER).convert('RGB')).astype(int)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    bg = np.array([251.4, 250.4, 233.7])
    # letters: orange components in the tummy area
    org = (r > 200) & (g > 40) & (g < 160) & (b < 120)
    cs, lab = comps(org, 800)
    low = sorted([c for c in cs if c['y'] > 450 and c['x'] < 420], key=lambda c: c['x'])
    S, I = low[0], low[1]
    tilt = angle_of(lab == I['i'])
    pad = 12
    def crop(c):
        return slice(c['y'] - pad, c['y'] + c['h'] + pad), slice(c['x'] - pad, c['x'] + c['w'] + pad)
    ys, xs = crop(S)
    d = np.abs(rgb[ys, xs] - bg).sum(-1)
    s_alpha = np.clip((d - 20) / 300.0, 0, 1) * (lab[ys, xs] == S['i'] if False else 1)
    s_alpha = np.where(cv2.dilate((lab[ys, xs] == S['i']).astype(np.uint8), np.ones((5, 5), np.uint8)) == 1, s_alpha, 0)
    s_img = Image.fromarray((s_alpha * 255).astype(np.uint8), 'L').rotate(tilt, resample=Image.BICUBIC, expand=True)
    s_img = s_img.crop(s_img.getbbox())
    ys, xs = crop(I)
    i_alpha = np.where(cv2.dilate((lab[ys, xs] == I['i']).astype(np.uint8), np.ones((5, 5), np.uint8)) == 1, np.clip((d.shape and np.abs(rgb[ys, xs] - bg).sum(-1) - 20) / 300.0, 0, 1), 0)
    i_img = Image.fromarray((i_alpha * 255).astype(np.uint8), 'L').rotate(tilt, resample=Image.BICUBIC, expand=True)
    i_img = i_img.crop(i_img.getbbox())
    # gap between S and I along the de-rotated baseline (measured from the master's own spacing)
    gap = max(2, round((I['x'] - (S['x'] + S['w'])) + 0.08 * I['h']))   # master spacing (its bboxes overlap because of the tilt)
    # pin: the highest pure-red component, with its hole
    red = (r > 200) & (g < 90) & (b < 90)
    pc, plab = comps(red, 2000)
    P = min(pc, key=lambda c: c['y'])
    ys, xs = slice(P['y'] - 4, P['y'] + P['h'] + 4), slice(P['x'] - 4, P['x'] + P['w'] + 4)
    dd = np.abs(rgb[ys, xs] - bg).sum(-1)
    near = cv2.dilate((plab[ys, xs] == P['i']).astype(np.uint8), np.ones((5, 5), np.uint8)) == 1
    p_alpha = np.where(near, np.clip((dd - 20) / 250.0, 0, 1), 0)
    p_rgb = rgb[ys, xs].astype(float)
    a = p_alpha[..., None]
    p_rgb = np.where((a > 0.02) & (a < 0.98), np.clip((p_rgb - (1 - a) * bg) / np.maximum(a, 1e-3), 0, 255), p_rgb)
    pin = Image.fromarray(np.dstack([p_rgb, p_alpha * 255]).astype(np.uint8), 'RGBA')
    pin = pin.crop(pin.getbbox())
    return s_img, i_img, gap / I['h'], S['h'] / I['h'], pin


def patch(name, parts, log):
    s_glyph, _, gap_ratio, sh_ratio, pin = parts
    im = np.array(Image.open(SRC / f'matebot_pose_{name}.png').convert('RGBA')).astype(int)
    H, W = im.shape[:2]
    r, g, b, a = im[..., 0], im[..., 1], im[..., 2], im[..., 3]
    red = (a > 200) & (r > 200) & (g < 120) & (b < 110)
    cs, lab = comps(red, 2000)
    # ---- antenna ball: the highest sizeable round red component
    balls = [c for c in cs if 0.8 < c['w'] / c['h'] < 1.25 and c['area'] / (c['w'] * c['h']) > 0.7]
    ball = min(balls, key=lambda c: c['y'])
    # ---- letters: the A / I pair in the lower half
    As = [c for c in cs if 95 < c['h'] < 160 and 0.8 < c['w'] / c['h'] < 1.4 and c['cy'] > H * 0.5 and c is not ball]
    Is = [c for c in cs if 95 < c['h'] < 160 and 0.28 < c['w'] / c['h'] < 0.7 and c['cy'] > H * 0.5]
    pair = None
    for A in As:
        for I in Is:
            if I['x'] > A['x'] + A['w'] * 0.7 and I['x'] - (A['x'] + A['w']) < 70 and abs(I['cy'] - A['cy']) < 0.4 * I['h']:
                pair = (A, I)
    if not pair:
        raise RuntimeError(f'{name}: AI letters not found')
    A, I = pair
    out = im.copy()
    # ---- 1. tummy: erase "AI", draw "SI"
    letter_mask = (lab == A['i']) | (lab == I['i'])
    i_sprite_mask = cv2.dilate((lab == I['i']).astype(np.uint8), np.ones((5, 5), np.uint8)) == 1
    tilt = angle_of(lab == I['i'])
    # colour of the letters and of the body around them
    letter_col = np.median(im[lab == I['i']][:, :3], axis=0)
    x0, y0 = max(0, A['x'] - 25), max(0, A['y'] - 25); x1, y1 = min(W, I['x'] + I['w'] + 25), min(H, I['y'] + I['h'] + 25)
    region = im[y0:y1, x0:x1]
    whiteish = (region[..., 3] > 200) & (region[..., 0] > 225) & (region[..., 1] > 225) & (region[..., 2] > 215)
    body_col = np.median(region[whiteish][:, :3], axis=0) if whiteish.any() else np.array([254, 250, 240])
    # keep the I as a sprite (its own anti-aliased pixels) before erasing
    i_alpha = np.zeros((H, W)); sel = i_sprite_mask
    # letter-ness of each pixel: distance from body colour relative to distance body->letter
    dist = np.abs(im[..., :3] - body_col).sum(-1) / max(1.0, np.abs(letter_col - body_col).sum())
    i_alpha[sel] = np.clip(dist[sel], 0, 1) * (a[sel] / 255.0)
    i_alpha[~(sel & (im[..., 0] > 150))] = 0  # never carry plum (a hand or outline) with the sprite
    # erase both letters: every reddish pixel around them becomes body colour (plum props / hands are left alone)
    erase = cv2.dilate(letter_mask.astype(np.uint8), np.ones((7, 7), np.uint8)) == 1
    erase &= (im[..., 0] > 150) & (a > 0)
    out[erase, :3] = body_col; out[erase, 3] = 255
    # layout: I keeps its height; S height from the master ratio; both centred where "AI" sat
    hI = I['h']; hS = round(hI * sh_ratio)
    s_img = s_glyph.resize((round(s_glyph.width * hS / s_glyph.height), hS), Image.LANCZOS)
    gap = round(gap_ratio * hI)
    i_crop = Image.fromarray((i_alpha[I['y'] - 3:I['y'] + I['h'] + 3, I['x'] - 3:I['x'] + I['w'] + 3] * 255).astype(np.uint8), 'L')
    # the pose lettering is chunkier than the master's: thicken the S stroke a little so S and I match in weight
    sa = np.array(s_img); kk = max(1, round(hS * 0.035)) * 2 + 1
    s_img = Image.fromarray(cv2.dilate(sa, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk))), 'L')
    s_rot = s_img.rotate(-tilt, resample=Image.BICUBIC, expand=True)
    def solid_bbox(img):
        m = np.array(img) > 128; ys_, xs_ = np.nonzero(m); return xs_.min(), ys_.min(), xs_.max() + 1, ys_.max() + 1
    sb, ib = solid_bbox(s_rot), solid_bbox(i_crop)
    total_w = (sb[2] - sb[0]) + gap + (ib[2] - ib[0])
    cx = (A['x'] + I['x'] + I['w']) / 2.0; cy = (min(A['y'], I['y']) + max(A['y'] + A['h'], I['y'] + I['h'])) / 2.0
    sx = round(cx - total_w / 2) - sb[0]; ix = sx + sb[2] + gap - ib[0]
    sy = round(cy - (sb[3] - sb[1]) / 2) - sb[1]; iy = round(cy - (ib[3] - ib[1]) / 2) - ib[1]
    visible = ((out[..., 0] > 200) & (out[..., 1] > 200) & (out[..., 2] > 190) & (out[..., 3] > 200)).astype(float)
    def stamp(alpha_img, x, y, col):
        al = np.array(alpha_img).astype(float) / 255.0
        h, w = al.shape; xa, ya = max(0, x), max(0, y); xb, yb = min(W, x + w), min(H, y + h)
        al = al[ya - y:yb - y, xa - x:xb - x] * visible[ya:yb, xa:xb]
        out[ya:yb, xa:xb, :3] = (out[ya:yb, xa:xb, :3] * (1 - al[..., None]) + np.array(col) * al[..., None]).round()
    stamp(s_rot, sx, sy, letter_col)
    stamp(i_crop, ix, iy, letter_col)
    # ---- 2. antenna: erase the ball, join the locator pin to the spring
    bm = cv2.dilate((lab == ball['i']).astype(np.uint8), np.ones((9, 9), np.uint8)) == 1
    dark = (a > 150) & (r < 140) & (g < 100) & (b < 150)
    bm &= ~dark                                       # the spring's top coil may touch the ball: keep it
    out[bm] = 0
    # motion-trail ghosts of the ball (a paler red ring beside it in the running pose): any reddish, non-dark pixel
    # near the old ball goes too; props elsewhere are outside this window
    wx0, wx1 = max(0, int(ball['x'] - ball['w'] * 0.7)), min(W, int(ball['x'] + ball['w'] * 1.7))
    wy0, wy1 = max(0, int(ball['y'] - ball['h'] * 0.5)), min(H, int(ball['y'] + ball['h'] * 1.1))
    win = out[wy0:wy1, wx0:wx1]
    ghost = (win[..., 3] > 0) & (win[..., 0] > 170) & (win[..., 0] > win[..., 1] + 12) & (win[..., 0] > win[..., 2] + 12) & ~dark[wy0:wy1, wx0:wx1]
    win[ghost] = 0
    if name == 'run':   # the running pose also has a pale glow ring around the old ball (nothing else is light up there)
        gx0, gy0 = max(0, int(ball['x'] - ball['w'] * 1.5)), max(0, int(ball['y'] - ball['h'] * 0.8))
        gy1 = min(H, int(ball['y'] + ball['h'] * 1.6))      # y≈353, still above the helmet top (y≈375) in this pose
        gwin = out[gy0:gy1, gx0:wx1]
        # everything up there except the dark spring is ball glow / motion ghost (semi-transparent pink and grey)
        glow = (gwin[..., 3] > 0) & ~dark[gy0:gy1, gx0:wx1]
        gwin[glow] = 0
    bx0, bx1 = int(ball['cx'] - ball['w'] * 0.6), int(ball['cx'] + ball['w'] * 0.6)
    by0, by1 = ball['y'] + ball['h'] // 2, min(H, ball['y'] + ball['h'] + 80)
    q = dark[by0:by1, bx0:bx1]
    ys, xs = np.nonzero(q)
    if not len(ys):
        raise RuntimeError(f'{name}: spring top not found')
    top = ys.min(); anchor_x = bx0 + float(np.median(xs[ys <= top + 2])); anchor_y = by0 + top
    scale = PIN_SCALE * (0.8 if name == 'star' else 1.0)   # the star sits right behind the antenna: keep the pin clear of it
    pw = max(30, round(ball['w'] * scale)); ph = round(pin.height * pw / pin.width)
    pin_s = pin.resize((pw, ph), Image.LANCZOS)
    pa = np.array(pin_s)
    tip_row = np.nonzero(pa[..., 3].max(1) > 128)[0].max(); tip_col = float(np.nonzero(pa[tip_row, :, 3] > 128)[0].mean())
    px = round(anchor_x - tip_col); py = round(anchor_y + 4 - tip_row)   # tip overlaps the spring top by ~4px
    pad_top = max(0, -py)
    if pad_top:
        out = np.concatenate([np.zeros((pad_top, W, 4), out.dtype), out]); py += pad_top; H += pad_top
    h, w = pa.shape[:2]; xa, ya = max(0, px), max(0, py); xb, yb = min(W, px + w), min(H, py + h)
    src = pa[ya - py:yb - py, xa - px:xb - px].astype(float); al = src[..., 3:4] / 255.0
    dst = out[ya:yb, xa:xb].astype(float)
    dst_a = dst[..., 3:4] / 255.0
    new_a = al + dst_a * (1 - al)
    new_rgb = (src[..., :3] * al + dst[..., :3] * dst_a * (1 - al)) / np.maximum(new_a, 1e-3)
    out[ya:yb, xa:xb, :3] = new_rgb.round(); out[ya:yb, xa:xb, 3] = (new_a[..., 0] * 255).round()
    # ---- 3. key pose: no key in his hand (P5)
    if name == 'key':
        rgbu = out[..., :3].astype(np.uint8)
        hsv = cv2.cvtColor(rgbu, cv2.COLOR_RGB2HSV)
        yellow = (cv2.inRange(hsv, np.array([12, 90, 140]), np.array([40, 255, 255])) > 0) & (out[..., 3] > 0)
        yellow[:, :560] = False; yellow[700:, :] = False
        yellow = cv2.dilate(yellow.astype(np.uint8), np.ones((5, 5), np.uint8)) == 1
        # the fist: dark blob around the key shaft; yellow inside its hull becomes hand colour, the rest disappears
        fist_win = np.zeros_like(yellow); fist_win[440:700, 780:1010] = True
        fist = dark & fist_win
        hull = np.zeros_like(yellow, dtype=np.uint8)
        pts = np.column_stack(np.nonzero(fist)[::-1]).astype(np.int32)
        cv2.fillConvexPoly(hull, cv2.convexHull(pts), 1)
        hand_col = np.median(out[fist][:, :3], axis=0)
        inside = yellow & (hull == 1) & ~dark
        out[inside, :3] = hand_col; out[inside, 3] = 255
        out[yellow & (hull == 0)] = 0
        log['key_removed'] = int(yellow.sum())
    log.update(ball=ball, A=A, I=I, tilt=round(tilt, 1), anchor=[round(anchor_x), round(anchor_y)], pin=[px, py, pw, ph])
    img = Image.fromarray(out.astype(np.uint8), 'RGBA')
    if img.height != 1536:
        img = img.resize((round(img.width * 1536 / img.height), 1536), Image.LANCZOS)
    img = img.resize((OUT_W, round(img.height * OUT_W / img.width)), Image.LANCZOS)
    if img.height != OUT_H:
        canvas = Image.new('RGBA', (OUT_W, max(OUT_H, img.height)), (0, 0, 0, 0)); canvas.alpha_composite(img); img = canvas
    img.save(OUT / f'matebot_{name}.webp', 'WEBP', quality=90, method=6)
    img.save(OUT / f'matebot_{name}.png', 'PNG', optimize=True)
    return img


def main(names=None):
    parts = master_parts()
    poses = sorted(p.stem.replace('matebot_pose_', '') for p in SRC.glob('matebot_pose_*.png'))
    if names: poses = [p for p in poses if p in names]
    logs = {}; thumbs = []
    for name in poses:
        logs[name] = {}
        img = patch(name, parts, logs[name]); thumbs.append((name, img))
        print(name, logs[name]['pin'], 'tilt', logs[name]['tilt'], flush=True)
    cols = 8; tw, th = 180, 270; rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * tw, rows * (th + 18)), (246, 241, 234)); d = ImageDraw.Draw(sheet)
    for k, (name, img) in enumerate(thumbs):
        t = img.copy(); t.thumbnail((tw, th)); x = (k % cols) * tw; y = (k // cols) * (th + 18)
        sheet.paste(t, (x, y), t); d.text((x + 4, y + th + 3), name, fill=(0, 0, 0))
    sheet.save('/tmp/matebot_poses_sheet.png')
    Path('/tmp/matebot_poses_log.json').write_text(json.dumps(logs, indent=1, default=str))


if __name__ == '__main__':
    main(sys.argv[1:] or None)
