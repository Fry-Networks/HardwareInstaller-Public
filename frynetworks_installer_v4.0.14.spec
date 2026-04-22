# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['core.service_manager', 'core.config_manager', 'core.conflict_detector', 'core.naming', 'core.key_parser']
hiddenimports += collect_submodules('core')


a = Analysis(
    ['installer_main.py'],
    pathex=['.', '.\\core', '.\\gui'],
    binaries=[],
    datas=[('build_config.json', '.'), ('resources\\background.png', 'resources'), ('resources\\frynetworks_logo.ico', 'resources'), ('resources\\embedded', 'resources\\embedded'), ('SDK', 'SDK'), ('core', 'core'), ('dist\\frynetworks_updater.exe', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
splash = Splash(
    'resources\\frynetworks_splash.png',
    binaries=a.binaries,
    datas=a.datas,
    text_pos='bottom-left',
    text_size=12,
    minify_script=True,
    always_on_top=True,
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    splash,
    splash.binaries,
    [],
    name='frynetworks_installer_v4.0.14',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon=['resources\\frynetworks_logo.ico'],
)
