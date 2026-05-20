"""
批量分析图片 - 将5张图片作为完整代码的不同部分进行分析
"""
import sys
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from analyze_image import analyze_image, print_result

# 图片路径列表（按顺序）
image_paths = [
    r"C:\Users\mading\Documents\WXWork\1688858240435547\Cache\Image\2026-05\9af2e6fc-b0f8-440d-b7a8-95ad64549271.jpg",
    r"C:\Users\mading\Documents\WXWork\1688858240435547\Cache\Image\2026-05\46ab4517-1974-4f1f-801b-535f3d5d89c4.jpg",
    r"C:\Users\mading\Documents\WXWork\1688858240435547\Cache\Image\2026-05\be97a52d-1af2-41fe-8df3-a82d347d9746.jpg",
    r"C:\Users\mading\Documents\WXWork\1688858240435547\Cache\Image\2026-05\10260e3a-2dc0-4b44-b46f-17e152c9a411.jpg",
    r"C:\Users\mading\Documents\WXWork\1688858240435547\Cache\Image\2026-05\81b6c421-cba0-4fa1-80e9-9d25fe1b5983.jpg",
]

all_results = []

for i, img_path in enumerate(image_paths):
    print(f"\n{'='*60}")
    print(f"分析图片 {i+1}/5: {os.path.basename(img_path)}")
    print(f"{'='*60}")
    
    prompt = (
        f"这是一组完整代码照片的第 {i+1} 张（共5张）。"
        f"请详细提取这张图片中显示的所有代码内容，包括："
        f"1）所有代码文本，精确到每个字符"
        f"2）代码结构和缩进"
        f"3）注释内容"
        f"4）如果能看到导入语句、函数定义、类定义等重点标注"
        f"请原样输出代码，不要省略任何部分。"
    )
    
    result = analyze_image(img_path, prompt=prompt)
    all_results.append(result)
    
    if result.get("success"):
        print(f"✅ 图片 {i+1} 分析成功")
        print(f"\n--- 图片 {i+1} 内容 ---")
        print(result.get("content", ""))
        print(f"--- 图片 {i+1} 结束 ---\n")
    else:
        print(f"❌ 图片 {i+1} 分析失败: {result.get('error')}")

print(f"\n\n{'='*60}")
print("所有图片分析完成！")
print(f"{'='*60}")
