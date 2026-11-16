Add-Type -AssemblyName System.Drawing

# 先按 256 画一张，再用高质量缩放得到 64（保证两个尺寸观感一致）
$bmd = New-Object System.Drawing.Bitmap(256, 256, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$gd = [System.Drawing.Graphics]::FromImage($bmd)
$gd.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$gd.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

$bgBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 15, 17, 21))
$gd.FillRectangle($bgBrush, 0, 0, 256, 256)

$edgePen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 44, 48, 58), 2)
$gd.DrawRectangle($edgePen, 3, 3, 249, 249)

$diskBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 232, 235, 240))
$gd.FillRectangle($diskBrush, 40, 74, 176, 100)

$slotBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 26, 29, 36))
$gd.FillRectangle($slotBrush, 56, 92, 26, 10)
$gd.FillRectangle($slotBrush, 94, 92, 26, 10)
$gd.FillRectangle($slotBrush, 132, 92, 26, 10)
$gd.FillRectangle($slotBrush, 170, 92, 26, 10)

$ledBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 92, 214, 148))
$gd.FillEllipse($ledBrush, 170, 132, 28, 28)

$accentBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 92, 214, 148))
$gd.FillRectangle($accentBrush, 40, 190, 176, 6)

$font = New-Object System.Drawing.Font("Microsoft YaHei UI", 26, [System.Drawing.FontStyle]::Bold)
$whiteBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 232, 235, 240))
$gd.DrawString("人话", $font, $whiteBrush, 40, 204)

$gd.Dispose()
$bmd.Save("D:\iot\扫盘\fpk\pkg\ICON_256.PNG", [System.Drawing.Imaging.ImageFormat]::Png)

$sm = New-Object System.Drawing.Bitmap(64, 64, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$sg = [System.Drawing.Graphics]::FromImage($sm)
$sg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$sg.DrawImage($bmd, 0, 0, 64, 64)
$sg.Dispose()
$sm.Save("D:\iot\扫盘\fpk\pkg\ICON.PNG", [System.Drawing.Imaging.ImageFormat]::Png)
$bmd.Dispose()
$sm.Dispose()
"OK " + (Get-Item "D:\iot\扫盘\fpk\pkg\ICON_256.PNG").Length + " / " + (Get-Item "D:\iot\扫盘\fpk\pkg\ICON.PNG").Length
