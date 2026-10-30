# Create the speech probe locally; generated system-voice audio is not distributed.
$ErrorActionPreference = 'Stop'
$assets = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\assets\capability')).Path
$manifestPath = Join-Path $assets 'manifest.json'
$target = Join-Path $assets 'audio-sample.wav'
$temporary = Join-Path $assets ('audio-sample-' + [guid]::NewGuid().ToString('N') + '.tmp.wav')
Add-Type -AssemblyName System.Speech
$speech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voice = $speech.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'zh-*'
    } | Select-Object -First 1
    if (-not $voice) { throw 'A Chinese system voice is required.' }
    $speech.SelectVoice($voice.VoiceInfo.Name)
    $speech.Rate = -1
    $speech.SetOutputToWaveFile($temporary)
    $phrase = '"\u8bf7\u6ce8\u610f\uff0c\u9a8c\u8bc1\u7801\u662f\u56db\u4e00\u4e03\uff0c\u9879\u76ee\u4ee3\u53f7\u6d77\u98ce\u3002"' | ConvertFrom-Json
    $speech.Speak($phrase)
    $speech.SetOutputToNull()
    Move-Item -LiteralPath $temporary -Destination $target -Force
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $manifest.'audio-sample.wav' = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json) + "`n", $utf8)
    Write-Output 'Speech sample generated locally.'
} finally {
    $speech.Dispose()
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
}
