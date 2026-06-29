
# make_ico.py
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

def make_placeholder(size=256, text="TB"):
    # 蜊倩牡閭梧勹�ｼ倶ｸｭ螟ｮ縺ｫ譁�蟄暦ｼ�TB = TarBackup�ｼ�
    img = Image.new("RGBA", (size, size), (35, 87, 137, 255))  # 豼�縺�繝悶Ν繝ｼ
    draw = ImageDraw.Draw(img)
    try:
        # 蜿ｯ閭ｽ縺ｪ繧牙ｰ代＠螟ｧ縺阪ａ縺ｮ繝輔か繝ｳ繝茨ｼ育腸蠅�縺ｫ繧医ｊ繝ｭ繝ｼ繝牙､ｱ謨励☆繧九％縺ｨ縺ゅｊ�ｼ�
        font = ImageFont.truetype("arial.ttf", 160)
    except Exception:
        font = ImageFont.load_default()
    # 譁�蟄励�ｮ謠冗判菴咲ｽｮ險育ｮ�
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - w) // 2, (size - h) // 2), text, fill=(255, 255, 255, 255), font=font)
    return img

def main():
    src = Path("assets/icon.png")             # 蜈･蜉娜NG�ｼ井ｻｻ諢擾ｼ�
    dst = Path("assets/icon.ico")             # 蜃ｺ蜉姜CO
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.exists():
        img = Image.open(src).convert("RGBA")
    else:
        # PNG縺檎┌縺代ｌ縺ｰ繝励Ξ繝ｼ繧ｹ繝帙Ν繝繝ｼ繧剃ｽ懊ｋ
        img = make_placeholder(256, "TBSafeBox")

    sizes = [(16,16), (24,24), (32,32), (48,48), (64,64), (128,128), (256,256)]
    img.save(dst, sizes=sizes)
    print("Saved:", dst)

if __name__ == "__main__":
    main()
