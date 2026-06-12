# Builds a synthetic demo meeting (two TTS voices on the "system" track, one
# on the "mic" track) so the pipeline can be tested without a real meeting.
param([string]$OutDir)

Add-Type -AssemblyName System.Speech
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $OutDir | Out-Null

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

# --- system.wav : two remote participants (David + Zira alternating) ---
$synth.SetOutputToWaveFile((Join-Path $OutDir "system.wav"), $fmt)
$synth.SelectVoice("Microsoft David Desktop")
$synth.Speak("Good morning everyone, thanks for joining the quarterly planning call. Um, I wanted to start with the budget review because, you know, we have some important decisions to make about the marketing spend this quarter.")
$synth.SelectVoice("Microsoft Zira Desktop")
$synth.Speak("Thanks David. I actually have the numbers right here. Revenue grew twelve percent compared to last quarter, but our customer acquisition cost went up as well. Should we consider moving some budget from paid advertising into content marketing?")
$synth.SelectVoice("Microsoft David Desktop")
$synth.Speak("That is a great question. Basically, I think we should run a small experiment first before committing the whole budget. Can you prepare a proposal by Friday?")
$synth.SelectVoice("Microsoft Zira Desktop")
$synth.Speak("Sure, I will draft the proposal and share it with the team by Thursday evening so everyone has time to review it before the deadline.")
$synth.SetOutputToNull()

# --- mic.wav : the local user ("You") ---
$synth.SetOutputToWaveFile((Join-Path $OutDir "mic.wav"), $fmt)
$synth.SelectVoice("Microsoft Zira Desktop")
$synth.Speak("Hello team, this is Sharique speaking from my side of the call. I agree with the experiment idea, and I can help set up the tracking dashboard for it next week.")
$synth.SetOutputToNull()
$synth.Dispose()

Write-Output "demo audio written to $OutDir"
