<#
  一键打包脚本：PyInstaller(单文件 + 无控制台) + UPX 压缩 + 数字签名 + 签名校验

  用法示例：
    # 默认：单文件、无黑框、UPX、自签名（本机测试）
    powershell -ExecutionPolicy Bypass -File .\build_exe.ps1

    # 正式发布：用受信任的代码签名证书
    powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -CertPath .\mycert.pfx -CertPassword 你的密码

    # 目录版（启动更快，不压缩成单文件）
    powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OneDir

    # 顺带生成中文名的副本
    powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OutputName "银豹版本下载链接速查"
#>
[CmdletBinding()]
param(
  [string]$Name = "PospalVersion",      # 可执行文件名（不含扩展名）
  [string]$OutputName = "",             # 非空时再复制一份为这个名字
  [string]$UpxDir = "D:\TOOLS",         # UPX 所在目录
  [string]$CertPath = "",               # .pfx 证书路径；留空则用自签名证书
  [string]$CertPassword = "",           # 证书密码
  [string]$TimestampUrl = "http://timestamp.digicert.com",
  [switch]$OneDir,                      # 目录版而非单文件
  [switch]$NoUpx,                       # 关闭 UPX 压缩
  [switch]$NoSign                       # 关闭签名
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 1) 选择 Python 解释器（优先项目自带 venv）
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "py" }
Write-Host "[Python] $py"

# 2) 清理旧产物
Remove-Item -Recurse -Force "$root\build", "$root\dist" -ErrorAction SilentlyContinue
Remove-Item -Force "$root\$Name.spec" -ErrorAction SilentlyContinue

# 3) 组织 PyInstaller 参数
$piArgs = @(
  "-m", "PyInstaller", "--noconfirm", "--clean",
  "--name", $Name,
  "--add-data", "templates;templates",
  "--collect-data", "pypinyin",
  "--collect-submodules", "pypinyin",
  "--exclude-module", "pytest",
  "--exclude-module", "tkinter.test",
  "--version-file", "version_info.txt"
)
if ($OneDir) { $piArgs += "--onedir" } else { $piArgs += "--onefile" }
$piArgs += "--windowed"                       # 关键：去掉控制台黑框

if (-not $NoUpx -and (Test-Path (Join-Path $UpxDir "upx.exe"))) {
  $piArgs += @("--upx-dir", $UpxDir)
  Write-Host "[UPX ] 使用 $UpxDir"
} else {
  $piArgs += "--noupx"
  Write-Host "[UPX ] 跳过（$UpxDir 下未找到 upx.exe）"
}
$piArgs += "gui.py"

Write-Host "[构建] pyinstaller $($piArgs -join ' ')"
& $py @piArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit $LASTEXITCODE）" }

# 4) 定位产物
$exe = if ($OneDir) { Join-Path $root "dist\$Name\$Name.exe" } else { Join-Path $root "dist\$Name.exe" }
if (-not (Test-Path $exe)) { throw "未找到产物：$exe" }

# 5) 签名
if ($NoSign) {
  Write-Host "[签名] 跳过"
} else {
  $signtool = (Get-ChildItem "C:\Program Files (x86)\Windows Kits\*\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1).FullName
  if (-not $signtool) { $signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source }
  if (-not $signtool) { throw "未找到 signtool.exe（需 Windows SDK）" }
  Write-Host "[签名] signtool: $signtool"

  if (-not $CertPath) {
    # 自签名证书：仅用于本机验证签名流程；对外发布请换成受信任的代码签名证书
    $subject = "CN=PospalVersion Dev"
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
            Where-Object { $_.Subject -eq $subject } | Select-Object -First 1
    if (-not $cert) {
      $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject `
        -CertStoreLocation Cert:\CurrentUser\My -KeyUsage DigitalSignature `
        -FriendlyName "PospalVersion Dev (self-signed)"
      Write-Host "[签名] 已创建自签名证书：$subject"
    }
    if (-not $CertPassword) { $CertPassword = "pospal" }
    $pfxDir = Join-Path $root "build"
    New-Item -ItemType Directory -Force -Path $pfxDir | Out-Null
    $CertPath = Join-Path $pfxDir "pospal_dev.pfx"
    if (-not (Test-Path $CertPath)) {
      $sec = ConvertTo-SecureString -String $CertPassword -Force -AsPlainText
      Export-PfxCertificate -Cert $cert -FilePath $CertPath -Password $sec | Out-Null
    }
    Write-Host "[签名] 使用自签名证书：$CertPath（对外发布请替换为受信任证书）"
  }

  $signArgs = @("sign", "/fd", "SHA256", "/f", $CertPath)
  if ($CertPassword) { $signArgs += @("/p", $CertPassword) }
  if ($TimestampUrl) { $signArgs += @("/tr", $TimestampUrl, "/td", "SHA256") }
  $signArgs += $exe
  & $signtool @signArgs
  if ($LASTEXITCODE -ne 0 -and $TimestampUrl) {
    Write-Host "[签名] 时间戳服务不可用，改为不带时间戳重新签名"
    $signArgs = @("sign", "/fd", "SHA256", "/f", $CertPath)
    if ($CertPassword) { $signArgs += @("/p", $CertPassword) }
    $signArgs += $exe
    & $signtool @signArgs
  }
  if ($LASTEXITCODE -ne 0) { throw "签名失败（exit $LASTEXITCODE）" }

  Write-Host "[校验] 数字签名"
  & $signtool verify /pa $exe
}

# 6) 可选：另存为指定文件名
if ($OutputName) {
  $dst = Join-Path $root "dist\$OutputName.exe"
  Copy-Item $exe $dst -Force
  Write-Host "[输出] $dst"
}

Write-Host ""
Write-Host "[完成] $exe"
Write-Host ("[大小] {0:N1} MB" -f ((Get-Item $exe).Length / 1MB))
