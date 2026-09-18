import os
import win32com.client

desktop = r"C:\Users\DAPCHC-071\Desktop"
base_dir = r"C:\Users\DAPCHC-071\namu-auto-trader"

shortcuts = [
    {
        "filename": "AI_Quant_Dual_Trader.lnk",
        "target": os.path.join(base_dir, "run_dual_trader.bat"),
        "description": "AI Quant 통합 듀얼 실시간 자동매매 (모의 + 실전 동시 가동)",
        "icon": r"%SystemRoot%\System32\shell32.dll,220"
    },
    {
        "filename": "AI_Quant_Launcher.lnk",
        "target": os.path.join(base_dir, "run_launcher.bat"),
        "description": "AI Quant 통합 런처 메뉴 v9.3",
        "icon": r"%SystemRoot%\System32\shell32.dll,25"
    },
    {
        "filename": "AI_Quant_Dashboard.lnk",
        "target": os.path.join(base_dir, "run_dashboard.bat"),
        "description": "AI Quant 실시간 웹 관제탑 (http://127.0.0.1:8080)",
        "icon": r"%SystemRoot%\System32\shell32.dll,14"
    },
    {
        "filename": "AI_Quant_Mock_Trader.lnk",
        "target": os.path.join(base_dir, "run_mock_trader.bat"),
        "description": "AI Quant 모의투자 실시간 자동매매 (계좌: 50001003032)",
        "icon": r"%SystemRoot%\System32\shell32.dll,13"
    },
    {
        "filename": "AI_Quant_Live_Trader.lnk",
        "target": os.path.join(base_dir, "run_live_trader.bat"),
        "description": "AI Quant 실전투자 실시간 자동매매 (계좌: 20201549311)",
        "icon": r"%SystemRoot%\System32\shell32.dll,145"
    }
]

shell = win32com.client.Dispatch("WScript.Shell")

for sc in shortcuts:
    p = os.path.join(desktop, sc["filename"])
    lnk = shell.CreateShortcut(p)
    lnk.TargetPath = sc["target"]
    lnk.WorkingDirectory = base_dir
    lnk.Description = sc["description"]
    lnk.IconLocation = sc["icon"]
    lnk.Save()
    print(f"Created: {p} -> {sc['target']}")
