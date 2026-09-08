import Foundation

// Wire shapes, matching Job.as_dict() in scribe/jobs.py. Decodable ignores the
// fields we don't list, so the server can grow without breaking the app.

struct Preset: Decodable {
    let label: String
    let backend: String?
}

struct JobInfo: Decodable {
    let id: String
    let name: String
    let status: String
    let stage: String
    let duration: Double
    let words: Int
    let paragraphs: Int
    let error: String
    let written: [String]
}

private struct JobsResponse: Decodable { let jobs: [JobInfo] }

enum APIError: Error { case badResponse(Int) }

enum API {
    private static func get<T: Decodable>(_ path: String, _ completion: @escaping (T?) -> Void) {
        let url = Server.baseURL.appendingPathComponent(path)
        URLSession.shared.dataTask(with: url) { data, _, _ in
            guard let data = data, let value = try? JSONDecoder().decode(T.self, from: data) else {
                completion(nil)
                return
            }
            completion(value)
        }.resume()
    }

    static func presets(_ completion: @escaping ([String: Preset]) -> Void) {
        get("api/presets") { (v: [String: Preset]?) in completion(v ?? [:]) }
    }

    static func jobs(_ completion: @escaping ([JobInfo]) -> Void) {
        get("api/jobs") { (v: JobsResponse?) in completion(v?.jobs ?? []) }
    }

    /// Upload a finished capture through the same /api/upload the browser uses.
    ///
    /// The multipart body is assembled in a temp file and streamed with
    /// `uploadTask(fromFile:)`, so an hour-long recording is never held in
    /// memory. The temp file is removed when the request completes.
    static func upload(file: URL, preset: String,
                       _ completion: @escaping (Result<JobInfo, Error>) -> Void) {
        let boundary = "scribe-\(UUID().uuidString)"
        let body = FileManager.default.temporaryDirectory
            .appendingPathComponent("scribe-upload-\(UUID().uuidString).bin")

        do {
            FileManager.default.createFile(atPath: body.path, contents: nil)
            let out = try FileHandle(forWritingTo: body)
            let head = "--\(boundary)\r\n"
                + "Content-Disposition: form-data; name=\"file\"; filename=\"\(file.lastPathComponent)\"\r\n"
                + "Content-Type: audio/wav\r\n\r\n"
            out.write(head.data(using: .utf8)!)
            let input = try FileHandle(forReadingFrom: file)
            while true {
                let chunk = input.readData(ofLength: 1 << 20)
                if chunk.isEmpty { break }
                out.write(chunk)
            }
            try input.close()
            out.write("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
            try out.close()
        } catch {
            completion(.failure(error))
            return
        }

        var comps = URLComponents(url: Server.baseURL.appendingPathComponent("api/upload"),
                                  resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "preset", value: preset)]
        var req = URLRequest(url: comps.url!)
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 300

        URLSession.shared.uploadTask(with: req, fromFile: body) { data, resp, err in
            try? FileManager.default.removeItem(at: body)
            if let err = err { completion(.failure(err)); return }
            let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
            guard code == 200, let data = data,
                  let job = try? JSONDecoder().decode(JobInfo.self, from: data) else {
                completion(.failure(APIError.badResponse(code)))
                return
            }
            completion(.success(job))
        }.resume()
    }
}
