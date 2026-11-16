# Revit 插件签名

打包时可以为 `TuMo.Revit.dll`、`TuMo.Core.dll` 和 `tumo-engine.exe` 添加 SHA-256 Authenticode 签名。证书和私钥必须在当前 Windows 用户的个人证书库中。`Install.ps1` 只复制文件，不修改 Windows 的证书信任设置。

```powershell
.\plugin\Package.ps1 -RevitVersion 2022 -SigningThumbprint '<证书指纹>' -OutputDirectory .\out\signing\packages
```

打包脚本会检查每个签名的发布者和状态。没有传入证书指纹时，仍可构建未签名包，GitHub 构建也仍使用此方式。

本地调试可以使用 TuMo 专用的自签名证书。空格去除后，证书指纹有 40 个十六进制字符。在证书尚未受信任时，可以给上面的命令加 `-AllowLocalTestCertificate`。这样只允许自签名证书遇到「根证书不受信任」这一个错误。这个开关不会添加信任，也不能用于给其他用户发布。

本机测试时，先核对公开证书的指纹，再将公开证书加入当前 Windows 用户的「受信任的根证书颁发机构」和「受信任的发布者」。然后将签名包解压，用 `Get-AuthenticodeSignature -LiteralPath .\TuMo\TuMo.Revit.dll` 检查 `Status` 是否显示 `Valid`。安装与 Revit 同年份的包，重启 Revit，检查 `TuMo` 标签是否出现。

参考 [Autodesk 的本地测试证书说明](https://help.autodesk.com/cloudhelp/2024/CHS/Revit-API/files/Revit_API_Developers_Guide/Introduction/Add_In_Integration/Digitally_Signing_Your_Revit_Add_in/Revit_API_Revit_API_Developers_Guide_Introduction_Add_In_Integration_Digitally_Signing_Your_Revit_Add_in_Making_Your_Own_Certificate_for_Testing_and_Internal_Use_html.html)。给其他用户发布时，应使用目标电脑信任的代码签名证书，并配置可信时间戳。私钥不可放入安装包或提交到 Git。
