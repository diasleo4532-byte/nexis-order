$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
}
if (-not (Test-Path $csc)) {
    throw "csc.exe (.NET Framework 4) was not found."
}

Add-Type -AssemblyName System.Drawing

$source = Join-Path $PSScriptRoot "NexisLauncher.cs"
$icon = Join-Path $PSScriptRoot "nexis.ico"
$output = Join-Path $root "Nexis App.exe"

function New-NexisPng([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap -ArgumentList $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $s = $size / 64.0

    $teal = [System.Drawing.Color]::FromArgb(255, 127, 215, 196)
    $blue = [System.Drawing.Color]::FromArgb(255, 106, 168, 255)
    $rect = New-Object System.Drawing.Rectangle -ArgumentList 0, 0, $size, $size
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush -ArgumentList $rect, $teal, $blue, 45
    $bg = New-Object System.Drawing.SolidBrush -ArgumentList ([System.Drawing.Color]::FromArgb(255, 11, 15, 20))

    $radius = 13 * $s
    $d = $radius * 2
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc(0, 0, $d, $d, 180, 90)
    $path.AddArc($size - $d, 0, $d, $d, 270, 90)
    $path.AddArc($size - $d, $size - $d, $d, $d, 0, 90)
    $path.AddArc(0, $size - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    $g.FillPath($bg, $path)

    $ring = New-Object System.Drawing.Pen -ArgumentList $brush, ([Math]::Max(1.0, 2.5 * $s))
    $g.DrawEllipse($ring, 6 * $s, 6 * $s, 52 * $s, 52 * $s)

    $tick = New-Object System.Drawing.Pen -ArgumentList $brush, ([Math]::Max(1.0, 2.5 * $s))
    $tick.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $tick.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $g.DrawLine($tick, 32 * $s, 6 * $s, 32 * $s, 14 * $s)
    $g.DrawLine($tick, 32 * $s, 50 * $s, 32 * $s, 58 * $s)
    $g.DrawLine($tick, 6 * $s, 32 * $s, 14 * $s, 32 * $s)
    $g.DrawLine($tick, 50 * $s, 32 * $s, 58 * $s, 32 * $s)

    $g.FillEllipse($brush, 25 * $s, 25 * $s, 14 * $s, 14 * $s)

    $stream = New-Object System.IO.MemoryStream
    $bmp.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bytes = $stream.ToArray()
    $stream.Dispose()
    $g.Dispose()
    $bmp.Dispose()
    return ,$bytes
}

$sizes = @(16, 24, 32, 48, 64, 128, 256)
$images = New-Object System.Collections.ArrayList
foreach ($size in $sizes) {
    [void]$images.Add((New-NexisPng $size))
}

$buffer = New-Object System.IO.MemoryStream
$writer = New-Object System.IO.BinaryWriter -ArgumentList $buffer
$writer.Write([UInt16]0)
$writer.Write([UInt16]1)
$writer.Write([UInt16]$sizes.Count)

$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $size = $sizes[$i]
    if ($size -ge 256) { $dimension = 0 } else { $dimension = $size }
    $data = [byte[]]$images[$i]
    $writer.Write([byte]$dimension)
    $writer.Write([byte]$dimension)
    $writer.Write([byte]0)
    $writer.Write([byte]0)
    $writer.Write([UInt16]1)
    $writer.Write([UInt16]32)
    $writer.Write([UInt32]$data.Length)
    $writer.Write([UInt32]$offset)
    $offset += $data.Length
}
foreach ($image in $images) {
    $writer.Write([byte[]]$image)
}
$writer.Flush()
[System.IO.File]::WriteAllBytes($icon, $buffer.ToArray())
$writer.Dispose()
$buffer.Dispose()

& $csc /nologo /target:winexe /optimize+ /r:System.Windows.Forms.dll "/win32icon:$icon" "/out:$output" $source
if ($LASTEXITCODE -ne 0) {
    throw "csc failed with exit code $LASTEXITCODE"
}

Write-Host "Built: $output"
