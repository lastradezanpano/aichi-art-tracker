# 愛知アート帳をローカルWebサーバーで確認するためのスクリプト
param(
    [int]$Port = 8766
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pageUrl = "http://127.0.0.1:$Port/index.html"

Write-Host "愛知アート帳を配信しています: $pageUrl"
Write-Host "終了するには Ctrl+C を押してください。"

Set-Location -LiteralPath $root
python -m http.server $Port --bind 127.0.0.1