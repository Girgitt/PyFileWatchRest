from cx_Freeze import Executable, setup

build_exe_options = {
    "packages": [
        "watchdog",
        "requests",
        "psutil"
    ],
    "include_files": [
        ("config.example.json", "config.example.json"),
        ("nssm.exe", "nssm.exe"),
        ("install_service.cmd", "install_service.cmd")
    ],
    "include_msvcr": True,
    "excludes": [
        "tkinter",
        "unittest",
    ],
}

setup(
    name="FileWatchRestPy",
    version="0.3.0",
    description="Two-stage directory watcher which posts created/modified files to a REST endpoint",
    options={"build_exe": build_exe_options},
    executables=[
        Executable(
            script="filewatchrest_main.py",
            target_name="PyFileWatchRest.exe",
            base=None,
        )
    ],
)

