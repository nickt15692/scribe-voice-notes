import AVFoundation

enum RecorderError: Error {
    case failedToStart
    case deviceUnavailable(String)
}

/// Microphone capture to a 16 kHz mono 16-bit WAV on disk.
///
/// Built on `AVCaptureSession` rather than `AVAudioRecorder` for one reason:
/// `AVAudioRecorder` always records from the *system default* input and offers
/// no way to choose. That made a Bluetooth headset or USB mic usable only by
/// changing the system-wide default in System Settings, which also redirects
/// every other app. A capture session binds to a specific `AVCaptureDevice`,
/// so the choice lives here.
///
/// 16 kHz mono is Whisper's native input, so the engine's ffmpeg step becomes
/// nearly a no-op, and the file streams to disk as it records — an hour is
/// ~115 MB on disk and nothing in memory.
final class Recorder: NSObject, AVCaptureFileOutputRecordingDelegate {

    /// Persisted device `uniqueID`. Empty means "whatever the system default
    /// is", which is also the sane fallback when a chosen device disappears.
    static let deviceKey = "inputDeviceID"

    private let session = AVCaptureSession()
    private let output = AVCaptureAudioFileOutput()
    private let queue = DispatchQueue(label: "scribe.capture")
    private var currentInput: AVCaptureDeviceInput?

    private(set) var fileURL: URL?
    private(set) var deviceName: String = ""

    /// Called if the microphone vanishes mid-recording — a headset powering
    /// off, or a dongle being unplugged. Without this the session simply stops
    /// and the recording ends silently, which is the worst possible outcome
    /// for something you were mid-sentence in.
    var onDeviceLost: ((String) -> Void)?

    var isRecording: Bool { output.isRecording }
    var elapsed: TimeInterval {
        let t = output.recordedDuration
        return t.isNumeric ? CMTimeGetSeconds(t) : 0
    }

    override init() {
        super.init()
        NotificationCenter.default.addObserver(
            self, selector: #selector(deviceDisconnected(_:)),
            name: .AVCaptureDeviceWasDisconnected, object: nil)
    }

    // MARK: devices

    /// Every audio input macOS can see. `.external` covers USB and Bluetooth
    /// interfaces; `.microphone` covers the built-in.
    static func inputDevices() -> [AVCaptureDevice] {
        // `.microphone` / `.external` are macOS 14+; the older spellings still
        // work below that and are what keeps the deployment target at 13.
        let types: [AVCaptureDevice.DeviceType]
        if #available(macOS 14.0, *) {
            types = [.microphone, .external]
        } else {
            types = [.builtInMicrophone, .externalUnknown]
        }
        return AVCaptureDevice.DiscoverySession(
            deviceTypes: types,
            mediaType: .audio, position: .unspecified).devices
    }

    /// The device to record from: the saved choice if it is still connected,
    /// otherwise the system default.
    static func selectedDevice() -> AVCaptureDevice? {
        let saved = UserDefaults.standard.string(forKey: deviceKey) ?? ""
        if !saved.isEmpty, let d = inputDevices().first(where: { $0.uniqueID == saved }) {
            return d
        }
        return AVCaptureDevice.default(for: .audio)
    }

    static func requestAccess(_ completion: @escaping (Bool) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            completion(true)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                DispatchQueue.main.async { completion(ok) }
            }
        default:
            completion(false)
        }
    }

    // MARK: recording

    @discardableResult
    func start() throws -> URL {
        guard let device = Recorder.selectedDevice() else {
            throw RecorderError.deviceUnavailable("No microphone available")
        }

        let dir = Log.dir.appendingPathComponent("capture")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        // The filename becomes the job name and the transcript's slug, so make
        // it something readable in a folder listing.
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd-HHmm"
        let url = dir.appendingPathComponent("note-\(f.string(from: Date())).wav")

        session.beginConfiguration()
        if let old = currentInput { session.removeInput(old) }
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else {
            session.commitConfiguration()
            throw RecorderError.deviceUnavailable(device.localizedName)
        }
        session.addInput(input)
        currentInput = input

        if !session.outputs.contains(output) {
            guard session.canAddOutput(output) else {
                session.commitConfiguration()
                throw RecorderError.failedToStart
            }
            session.addOutput(output)
        }
        session.commitConfiguration()

        // Ask for Whisper's native format directly, so no resampling is needed
        // downstream regardless of what the device natively runs at — Bluetooth
        // headsets in particular often sit at 16 or 24 kHz already.
        output.audioSettings = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]

        if !session.isRunning { session.startRunning() }
        output.startRecording(to: url, outputFileType: .wav, recordingDelegate: self)

        fileURL = url
        deviceName = device.localizedName
        Log.write("recording from '\(device.localizedName)' to \(url.lastPathComponent)")
        return url
    }

    /// Stop and hand back the file. Returns nil if nothing was recording.
    ///
    /// `stopRecording` is asynchronous — the delegate callback below is what
    /// actually flushes the file — so callers must not read the file until
    /// `onFinish` fires.
    func stop() -> URL? {
        guard output.isRecording else { return nil }
        let url = fileURL
        Log.write("stopping after \(clock(elapsed))")
        output.stopRecording()
        return url
    }

    /// Fired once the file is closed and safe to upload.
    var onFinish: ((URL) -> Void)?

    func fileOutput(_ output: AVCaptureFileOutput,
                    didFinishRecordingTo outputFileURL: URL,
                    from connections: [AVCaptureConnection],
                    error: Error?) {
        queue.async { [weak self] in
            if self?.session.isRunning == true { self?.session.stopRunning() }
        }
        if let error = error {
            // A device lost mid-recording still leaves a usable partial file,
            // so report the error but hand the file over anyway.
            Log.write("recording ended with error: \(error.localizedDescription)")
        }
        DispatchQueue.main.async { self.onFinish?(outputFileURL) }
    }

    @objc private func deviceDisconnected(_ note: Notification) {
        guard let lost = note.object as? AVCaptureDevice,
              lost.uniqueID == currentInput?.device.uniqueID else { return }
        Log.write("input device disconnected mid-recording: \(lost.localizedName)")
        if output.isRecording { output.stopRecording() }
        DispatchQueue.main.async { self.onDeviceLost?(lost.localizedName) }
    }
}
