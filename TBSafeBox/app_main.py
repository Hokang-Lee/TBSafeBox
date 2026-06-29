
# app_main.py（プロジェクト直下に配置）
# 目的：scripts パッケージの example_main を「パッケージとして」起動する

def main():
    # パッケージとして import
    from scripts.example_main import main as run_main
    run_main()

if __name__ == "__main__":
    main()
