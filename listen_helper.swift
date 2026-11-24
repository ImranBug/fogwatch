// Voice input for listen.py: push-to-talk hotkey (listen-only event tap), mic, on-device speech-to-text.
// Compiled on first use to data/listen-helper with an embedded Info.plist (its own Microphone / Speech / Input Monitoring grants).
// It NEVER posts, injects or modifies input events: the tap is kCGEventTapOptionListenOnly and every event passes through untouched.
// stdout (JSON lines): {"status": ready|denied|error|eof, ...} {"ptt": "down"|"up"} {"partial": t} {"final": t}
//                      {"chunk": wav, "dur": s} (cloud fallback only, when on-device recognition is unavailable)
// stdin: "mode ptt|always|off"  "key key|mouse CODE MODS"  "pause"  "resume"; EOF = listen.py gone -> exit.
// Args: --mode M --key "key|mouse CODE MODS" [--file AUDIO] (instead of the mic; in ptt mode = one press) [--chunks] [--chunk-dir D] [--words a,b]
import AVFoundation
import CoreGraphics
import Foundation
import Speech

// macOS asks the *responsible* process for consent. Re-spawn with responsibility disclaimed so that is this binary (with its own
// usage strings), not the terminal app (which may lack NSSpeechRecognitionUsageDescription and then gets killed by TCC).
@_silgen_name("responsibility_spawnattrs_setdisclaim")
func responsibility_spawnattrs_setdisclaim(_ attrs: UnsafeMutablePointer<posix_spawnattr_t?>, _ disclaim: Int32) -> Int32

if ProcessInfo.processInfo.environment["LISTEN_CHILD"] == nil {
    var attr: posix_spawnattr_t? = nil
    posix_spawnattr_init(&attr)
    _ = responsibility_spawnattrs_setdisclaim(&attr, 1)
    setenv("LISTEN_CHILD", "1", 1)
    var pid: pid_t = 0
    let rc = posix_spawn(&pid, CommandLine.arguments[0], nil, &attr, CommandLine.unsafeArgv, environ)
    if rc != 0 { FileHandle.standardError.write("listen-helper: posix_spawn failed \(rc)\n".data(using: .utf8)!); exit(1) }
    var st: Int32 = 0
    waitpid(pid, &st, 0)
    exit((st & 0x7f) == 0 ? (st >> 8) & 0xff : 128 + (st & 0x7f))
}

let outLock = NSLock()
func out(_ d: [String: Any]) {
    guard let j = try? JSONSerialization.data(withJSONObject: d) else { return }
    outLock.lock(); FileHandle.standardOutput.write(j + "\n".data(using: .utf8)!); outLock.unlock()
}
func arg(_ k: String) -> String? {
    let a = CommandLine.arguments
    return a.firstIndex(of: k).flatMap { $0 + 1 < a.count ? a[$0 + 1] : nil }
}

let filePath = arg("--file")
let words = (arg("--words") ?? "").split(separator: ",").map(String.init)
let chunkDir = arg("--chunk-dir") ?? NSTemporaryDirectory()
let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
var useCloud = CommandLine.arguments.contains("--chunks") || recognizer?.supportsOnDeviceRecognition != true
let END_S = 1.2, RESTART_S = 50.0  // always mode: utterance end = no new words for 1.2 s; Apple caps a task at ~1 min
var mode = arg("--mode") ?? "off"  // ptt | always | off
var pttKind = "mouse", pttCode: Int64 = 3, pttMods: UInt64 = 0
let lock = NSLock()  // guards req / recording / paused / samples (audio thread vs main)
var req: SFSpeechAudioBufferRecognitionRequest?
var task: SFSpeechRecognitionTask?
var recording = false, paused = false, samples: [Float] = [], rate = 16000.0
var text = "", lastChange = Date(), started = Date(), gen = 0, dead = false, finishing = false, finishAt = Date()
var held = false, engine: AVAudioEngine?, tapPort: CFMachPort?

func setKey(_ s: String) {
    let p = s.split(separator: " ").map(String.init)
    if p.count == 3, let c = Int64(p[1]), let m = UInt64(p[2]) { pttKind = p[0]; pttCode = c; pttMods = m }
}
if let k = arg("--key") { setKey(k) }

// ---- on-device recognition (main queue) -----------------------------------------------------------------------------
func newTask() {
    gen += 1
    let g = gen, r = SFSpeechAudioBufferRecognitionRequest()
    r.requiresOnDeviceRecognition = true
    r.shouldReportPartialResults = true
    r.contextualStrings = words
    task?.cancel()
    lock.lock(); req?.endAudio(); req = r; lock.unlock()
    text = ""; started = Date(); dead = false
    task = recognizer!.recognitionTask(with: r) { res, err in
        guard g == gen else { return }  // a cancelled task's late callback
        if let res = res {
            let t = res.bestTranscription.formattedString
            if t != text { text = t; lastChange = Date(); out(["partial": t]) }
        }
        if let e = err as NSError?, e.code == 201 {  // "Siri and Dictation are disabled": use the cloud fallback
            if !useCloud { useCloud = true; out(["status": "ready", "ondevice": false, "reason": "dictation disabled"]) }
        }
        if err != nil || res?.isFinal == true { dead = true }
    }
}
func stopTask() {
    gen += 1; task?.cancel(); task = nil
    lock.lock(); req?.endAudio(); req = nil; lock.unlock()
    text = ""
}
func flush() {
    if !text.isEmpty { out(["final": text]) }
    newTask()
}

// ---- audio ----------------------------------------------------------------------------------------------------------
func wav(_ s: [Float], _ path: String) {
    var d = Data(), sr = UInt32(rate)
    func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
    func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
    d.append("RIFF".data(using: .ascii)!); u32(36 + UInt32(s.count * 2)); d.append("WAVEfmt ".data(using: .ascii)!)
    u32(16); u16(1); u16(1); u32(sr); u32(sr * 2); u16(2); u16(16); d.append("data".data(using: .ascii)!); u32(UInt32(s.count * 2))
    for v in s { u16(UInt16(bitPattern: Int16(max(-1, min(1, v)) * 32767))) }
    try? d.write(to: URL(fileURLWithPath: path))
}
var nChunk = 0
func emitChunk(_ s: [Float]) {
    nChunk += 1
    let p = (chunkDir as NSString).appendingPathComponent("chunk\(getpid())_\(nChunk % 8).wav")
    wav(s, p)
    out(["chunk": p, "dur": Double(s.count) / rate])
}
// always mode + cloud: energy VAD so silence is never uploaded
var vadOn = false, vadSil = 0.0, vadSpeech = 0.0, floorRms: Float = 0.005, pre: [Float] = []
func vad(_ s: [Float]) {
    let dt = Double(s.count) / rate, rms = sqrt(s.reduce(0) { $0 + $1 * $1 } / Float(max(s.count, 1)))
    let loud = rms > max(0.01, floorRms * 3)
    if !vadOn { floorRms = 0.95 * floorRms + 0.05 * rms }
    if loud { vadSil = 0; vadSpeech += dt; if !vadOn { vadOn = true; samples = pre } } else if vadOn { vadSil += dt }
    if vadOn { samples += s } else { pre = Array((pre + s).suffix(Int(rate * 0.3))) }
    if vadOn && (vadSil >= END_S || Double(samples.count) / rate >= 15) {
        if vadSpeech >= 0.4 { emitChunk(samples) }
        vadOn = false; vadSpeech = 0; vadSil = 0; samples = []; pre = []
    }
}
func feed(_ b: AVAudioPCMBuffer) {  // audio thread
    lock.lock(); defer { lock.unlock() }
    if !recording || paused { return }
    if !useCloud { req?.append(b); return }
    guard let ch = b.floatChannelData?[0] else { return }
    let s = Array(UnsafeBufferPointer(start: ch, count: Int(b.frameLength)))
    if mode == "always" { vad(s) } else { samples += s }
}
func setMic(_ on: Bool) {
    if filePath != nil || on == (engine != nil) { return }
    if on {
        let e = AVAudioEngine(), input = e.inputNode, fmt = input.outputFormat(forBus: 0)
        rate = fmt.sampleRate
        input.installTap(onBus: 0, bufferSize: 2048, format: fmt) { b, _ in feed(b) }
        do { e.prepare(); try e.start(); engine = e } catch { out(["status": "error", "error": "mic: \(error)"]) }
    } else {
        engine?.inputNode.removeTap(onBus: 0); engine?.stop(); engine = nil
    }
}
func setRecording(_ on: Bool) { lock.lock(); recording = on; if on { samples = [] }; lock.unlock() }

// ---- modes ------------------------------------------------------------------------------------------------------------
func pttDown() {
    guard mode == "ptt", !finishing else { return }
    if !useCloud { newTask() }
    setRecording(true)
    setMic(true)
    out(["ptt": "down"])
}
func pttUp() {
    guard mode == "ptt", recording else { return }
    out(["ptt": "up"])
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {  // keep the last word
        guard mode == "ptt" else { return }
        setRecording(false)
        setMic(false)
        if useCloud {
            lock.lock(); let s = samples; samples = []; lock.unlock()
            if Double(s.count) / rate >= 0.3 { emitChunk(s) } else { out(["final": ""]) }
        } else {
            lock.lock(); req?.endAudio(); lock.unlock()
            finishing = true; finishAt = Date().addingTimeInterval(2)
        }
    }
}
func setMode(_ m: String) {
    if m == mode { return }
    if held && mode == "ptt" { pttUp() }
    mode = m
    stopTask(); setRecording(m == "always"); setMic(m == "always")
    if m == "always" && !useCloud { newTask() }
}

// ---- push-to-talk hotkey: LISTEN-ONLY event tap (never posts, injects or changes events) -----------------------------
func tapCB(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent, refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        if let t = tapPort { CGEvent.tapEnable(tap: t, enable: true) }
    } else {
        var down: Bool? = nil
        if pttKind == "mouse" && (type == .otherMouseDown || type == .otherMouseUp) && event.getIntegerValueField(.mouseEventButtonNumber) == pttCode {
            down = type == .otherMouseDown
        } else if pttKind == "key" && (type == .keyDown || type == .keyUp) && event.getIntegerValueField(.keyboardEventKeycode) == pttCode
                    && event.getIntegerValueField(.keyboardEventAutorepeat) == 0 {
            down = type == .keyDown
        }
        if let d = down, d != held, !d || (event.flags.rawValue & pttMods) == pttMods {
            held = d
            d ? pttDown() : pttUp()
        }
    }
    return Unmanaged.passUnretained(event)  // the event goes on to the game exactly as it came
}
func startTap() -> Bool {
    if !CGPreflightListenEventAccess() { _ = CGRequestListenEventAccess() }  // Input Monitoring prompt (keyboard keys need it)
    let types: [CGEventType] = [.keyDown, .keyUp, .otherMouseDown, .otherMouseUp]
    let mask = types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
    guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
                                    eventsOfInterest: mask, callback: tapCB, userInfo: nil) else { return false }
    tapPort = t
    CFRunLoopAddSource(CFRunLoopGetMain(), CFMachPortCreateRunLoopSource(nil, t, 0), .commonModes)
    CGEvent.tapEnable(tap: t, enable: true)
    return true
}

func begin() {
    let tap = filePath == nil ? startTap() : false
    Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { _ in
        let now = Date()
        if finishing {  // ptt released: wait for the final result (or 2 s)
            if dead || now >= finishAt { finishing = false; out(["final": text]); stopTask() }
            return
        }
        guard mode == "always", !useCloud, recording else { return }
        if (dead && now.timeIntervalSince(started) >= 1) || (!text.isEmpty && now.timeIntervalSince(lastChange) >= END_S)
            || now.timeIntervalSince(started) >= RESTART_S + 5 { flush() }
        else if text.isEmpty && now.timeIntervalSince(started) >= RESTART_S { newTask() }
    }
    let m = mode
    mode = "off"
    setMode(m)
    out(["status": "ready", "ondevice": !useCloud, "source": filePath == nil ? "mic" : "file", "tap": tap,
         "input_monitoring": CGPreflightListenEventAccess()])
    if let f = filePath {
        Thread.detachNewThread {
            guard let af = try? AVAudioFile(forReading: URL(fileURLWithPath: f)) else { out(["status": "error", "error": "bad file"]); exit(2) }
            rate = af.processingFormat.sampleRate
            if mode == "ptt" { DispatchQueue.main.sync { held = true; pttDown() } }
            while true {
                guard let b = AVAudioPCMBuffer(pcmFormat: af.processingFormat, frameCapacity: 4096) else { break }
                if (try? af.read(into: b)) == nil || b.frameLength == 0 { break }
                feed(b)
                Thread.sleep(forTimeInterval: Double(b.frameLength) / rate)
            }
            if mode == "ptt" { DispatchQueue.main.sync { held = false; pttUp() } }
            let z = AVAudioPCMBuffer(pcmFormat: af.processingFormat, frameCapacity: AVAudioFrameCount(rate * 0.1))!
            z.frameLength = z.frameCapacity  // zero-filled trailing silence for the VAD
            for _ in 0..<20 { feed(z); Thread.sleep(forTimeInterval: 0.1) }
            Thread.sleep(forTimeInterval: 2)
            out(["status": "eof"]); exit(0)
        }
    }
}

func withMic(_ f: @escaping () -> Void) {
    if filePath != nil { return f() }
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        DispatchQueue.main.async {
            if !ok { out(["status": "denied", "what": "mic"]); exit(4) }
            f()
        }
    }
}

Thread.detachNewThread {  // commands from listen.py
    while let line = readLine() {
        let p = line.trimmingCharacters(in: .whitespaces)
        DispatchQueue.main.async {
            if p.hasPrefix("mode ") { setMode(String(p.dropFirst(5))) }
            else if p.hasPrefix("key ") { setKey(String(p.dropFirst(4))) }
            else if p == "pause" || p == "resume" {
                lock.lock(); paused = p == "pause"; lock.unlock()
                if p == "pause" && mode == "always" && !useCloud { flush() }  // keep what was said before the coach spoke
            }
        }
    }
    exit(0)
}

if useCloud {
    withMic(begin)
} else {
    SFSpeechRecognizer.requestAuthorization { st in
        DispatchQueue.main.async {
            if st != .authorized { out(["status": "denied", "what": "speech", "code": st.rawValue]); exit(3) }
            // probe: an empty task fails at once with 201 when Siri & Dictation are off -> cloud fallback
            newTask(); lock.lock(); req?.endAudio(); lock.unlock()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { stopTask(); withMic(begin) }
        }
    }
}
RunLoop.main.run()
