"""Generate version_info.txt (a Windows VS_VERSIONINFO resource) for PyInstaller
from APP_VERSION in config.py. Proper exe metadata helps SmartScreen/SAC
reputation and makes the file's Properties -> Details show real values."""
import re
import io

m = re.search(r'(?m)^APP_VERSION\s*=\s*["\']([^"\']+)["\']', io.open("config.py", encoding="utf-8").read())
ver = m.group(1) if m else "1.0"
parts = [int(n) for n in re.findall(r"\d+", ver)][:4]
while len(parts) < 4:
    parts.append(0)
vt = tuple(parts)

COMPANY = "mournyl"
PRODUCT = "mournyl TwitchMiner"
DESC = "mournyl TwitchMiner"
COPYRIGHT = "mournyl"
EXE = "mournylTwitchMiner.exe"

text = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vt},
    prodvers={vt},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', '{COMPANY}'),
            StringStruct('FileDescription', '{DESC}'),
            StringStruct('FileVersion', '{ver}'),
            StringStruct('InternalName', '{EXE}'),
            StringStruct('LegalCopyright', '{COPYRIGHT}'),
            StringStruct('OriginalFilename', '{EXE}'),
            StringStruct('ProductName', '{PRODUCT}'),
            StringStruct('ProductVersion', '{ver}')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
io.open("version_info.txt", "w", encoding="utf-8").write(text)
print(f"version_info.txt written for v{ver} {vt}")
