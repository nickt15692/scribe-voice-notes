import AVFoundation

enum RecorderError: Error { case failedToStart }

/// Microphone capture straight to a 16 kHz mono 16-bit WAV on disk.
///
/// AVAudioRecorder rather than an AVAudioEngine tap: it does the sample-rate
/// and channel conversion itself and streams to the file as it goes, so an
/// hour of speech is ~115 MB on disk and nothing in memory. 16 kHz mono is
/// Whisper's native input, so the engine's ffmpeg step becomes a near no-op.
final class Recorder: NSObject, AVAudioRecorderDelegate {
    private var recorder: AVAudioRecorder?
    private(set) var fileURL: URL?

    var isRecording: Bool { recorder?.isRecording ?? false }
    var elapsed: TimeInterval { recorder?.currentTime ?? 0 }

    /// Resolve the microphone permission. The first call shows the macOS
    /// prompt; every later call answers immediately.
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

    @discardableResult
    func start() throws -> URL {
        let dir = Log.dir.appendingPathComponent("capture")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        // The filename becomes the job name and the transcript's slug, so make
        // it something you can read in a folder listing.
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd-HHmm"
        let url = dir.appendingPathComponent("note-\(f.string(from: Date())).wav")

        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]

        let r = try AVAudioRecorder(url: url, settings: settings)
        r.delegate = self
        guard r.prepareToRecord(), r.record() else {
            throw RecorderError.failedToStart
        }
        recorder = r
        fileURL = url
        Log.write("recording to \(url.lastPathComponent)")
        return url
    }

    /// Stop and hand back the file. Returns nil if nothing was recording.
    func stop() -> URL? {
        guard let r = recorder else { return nil }
        r.stop()
        recorder = nil
        Log.write("recording stopped after \(clock(r.currentTime))")
        return fileURL
    }

    func audioRecorderEncodeErrorDidOccur(_ recorder: AVAudioRecorder, error: Error?) {
        Log.write("recorder encode error: \(String(describing: error))")
    }
}
