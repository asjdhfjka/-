"""
批量将 data/ 下的 .doc 文件转成 .docx
依赖：LibreOffice（已安装）
使用：python convert_doc.py
"""
import subprocess
from pathlib import Path

# ⭐ 已确认路径
SOFFICE = r"C:\Program Files\LibreOffice\program\soffice.exe"

data_dir = Path("./data")
doc_files = list(data_dir.glob("*.doc"))

if not doc_files:
    print("✅ 没有找到 .doc 文件，无需转换")
    exit()

print(f"📂 找到 {len(doc_files)} 个 .doc 文件，开始转换...\n")

success = 0
failed = 0

for doc_file in doc_files:
    print(f"🔄 转换: {doc_file.name}")
    result = subprocess.run(
        [
            SOFFICE,
            "--headless",
            "--convert-to", "docx",
            "--outdir", str(data_dir),
            str(doc_file),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode == 0:
        print(f"   ✅ 成功 → {doc_file.stem}.docx")
        success += 1
    else:
        print(f"   ❌ 失败: {result.stderr}")
        failed += 1

print(f"\n{'=' * 50}")
print(f"🎉 转换完成：成功 {success}，失败 {failed}")
print(f"{'=' * 50}")
print("\n下一步：")
print("  1. 删除原有的 .doc 文件（否则会重复入库）")
print("  2. 运行 python rag_test.py 重建知识库")