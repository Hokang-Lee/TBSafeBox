import sys
from pathlib import Path

if getattr(sys, 'frozen', False):
    base = Path(sys._MEIPASS)
else:
    base = Path(__file__).resolve().parent.parent

if str(base) not in sys.path:
    sys.path.insert(0, str(base))


import tkinter as tk
from scripts.tar_gui import TarTab

def main():
    root = tk.Tk()
    root.title("TBSafeBox  Backup/Restore (v3.4 Health+Retention)")
    tab = TarTab(root, config_path="config/tar_defaults.json")
    tab.pack(fill="both", expand=True)
    root.mainloop()

if __name__ == "__main__":
    main()