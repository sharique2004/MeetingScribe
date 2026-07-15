// apple_llm — MeetingScribe's on-device language model helper.
//
// Wraps Apple's Foundation Models framework (macOS 26+, Apple Intelligence):
// the ~3B on-device model that runs on the Neural Engine. Structured output
// is enforced with guided generation, so callers always get schema-valid JSON.
//
//   apple_llm check
//     -> {"available": true} or {"available": false, "reason": "<code>"}
//
//   apple_llm serve
//     NDJSON request/response loop on stdin/stdout. One request per line:
//       {"id": 1, "instructions": "...", "prompt": "...",
//        "schema": {...}, "temperature": 0.2, "max_tokens": 1200}
//     One response per line:
//       {"id": 1, "ok": true, "result": {...}}
//       {"id": 1, "ok": false, "error": "context_overflow", "detail": "..."}
//
// The "schema" value is a small JSON-schema-like subset built by local_llm.py:
//   {"type":"object","name":"X","properties":[{"name":"a","type":"string",
//    "description":"...","optional":false}, ...]}
//   {"type":"array","items":{...},"min":1,"max":10}
//   {"type":"enum","choices":["a","b"]}
//   {"type":"string"} / {"type":"integer"} / {"type":"number"} / {"type":"boolean"}
//
// Compiled by swift_helpers.ensure_binary() to ~/.meetingscribe/bin/apple_llm.

import Foundation
import FoundationModels

// ----------------------------------------------------------------- output --

let emitLock = NSLock()

func emitLine(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: []),
          let line = String(data: data, encoding: .utf8) else {
        FileHandle.standardOutput.write("{\"ok\":false,\"error\":\"encode_failure\"}\n".data(using: .utf8)!)
        return
    }
    emitLock.lock()  // concurrent tasks: one whole line at a time
    FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
    emitLock.unlock()
}

// ----------------------------------------------------------- availability --

// Permissive guardrails: this helper only TRANSFORMS the user's own content
// (summarizing their meetings, tidying their transcripts, judging their own
// interview answers) — Apple provides this mode precisely so ordinary
// real-world talk (medical, legal, security topics…) isn't refused. The
// default guardrails rejected genuine meeting transcripts.
let llmModel = SystemLanguageModel(guardrails: .permissiveContentTransformations)

func availabilityInfo() -> [String: Any] {
    switch llmModel.availability {
    case .available:
        return ["available": true]
    case .unavailable(let reason):
        let code: String
        switch reason {
        case .appleIntelligenceNotEnabled: code = "apple_intelligence_disabled"
        case .deviceNotEligible: code = "device_not_eligible"
        case .modelNotReady: code = "model_not_ready"
        @unknown default: code = "unavailable"
        }
        return ["available": false, "reason": code]
    @unknown default:
        return ["available": false, "reason": "unavailable"]
    }
}

// -------------------------------------------------------------- schema in --

enum SchemaError: Error, CustomStringConvertible {
    case bad(String)
    var description: String { if case .bad(let s) = self { return s }; return "bad schema" }
}

func buildSchema(_ spec: [String: Any], fallbackName: String) throws -> DynamicGenerationSchema {
    let type = spec["type"] as? String ?? "string"
    let name = spec["name"] as? String ?? fallbackName
    let desc = spec["description"] as? String
    switch type {
    case "object":
        var props: [DynamicGenerationSchema.Property] = []
        for p in (spec["properties"] as? [[String: Any]] ?? []) {
            guard let pname = p["name"] as? String else { throw SchemaError.bad("property missing name") }
            let sub = try buildSchema(p, fallbackName: name + "_" + pname)
            props.append(.init(name: pname,
                               description: p["description"] as? String,
                               schema: sub,
                               isOptional: p["optional"] as? Bool ?? false))
        }
        return DynamicGenerationSchema(name: name, description: desc, properties: props)
    case "array":
        let items = spec["items"] as? [String: Any] ?? ["type": "string"]
        return DynamicGenerationSchema(arrayOf: try buildSchema(items, fallbackName: name + "Item"),
                                       minimumElements: spec["min"] as? Int,
                                       maximumElements: spec["max"] as? Int)
    case "enum":
        let choices = spec["choices"] as? [String] ?? []
        guard !choices.isEmpty else { throw SchemaError.bad("enum without choices") }
        return DynamicGenerationSchema(name: name, description: desc, anyOf: choices)
    case "string":  return DynamicGenerationSchema(type: String.self)
    case "integer": return DynamicGenerationSchema(type: Int.self)
    case "number":  return DynamicGenerationSchema(type: Double.self)
    case "boolean": return DynamicGenerationSchema(type: Bool.self)
    default: throw SchemaError.bad("unknown schema type: \(type)")
    }
}

// ------------------------------------------------------------------ serve --

func errorCode(for error: LanguageModelSession.GenerationError) -> String {
    switch error {
    case .exceededContextWindowSize: return "context_overflow"
    case .guardrailViolation: return "guardrail"
    case .unsupportedLanguageOrLocale: return "unsupported_language"
    case .assetsUnavailable: return "model_unavailable"
    case .rateLimited: return "rate_limited"
    case .concurrentRequests: return "busy"
    case .refusal: return "refusal"
    case .decodingFailure: return "decoding_failure"
    case .unsupportedGuide: return "bad_schema"
    @unknown default: return "generation_error"
    }
}

func handle(_ line: String) async {
    guard let data = line.data(using: .utf8),
          let req = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
        emitLine(["ok": false, "error": "bad_request", "detail": "request is not a JSON object"])
        return
    }
    let id = req["id"] ?? NSNull()
    guard let prompt = req["prompt"] as? String, !prompt.isEmpty else {
        emitLine(["id": id, "ok": false, "error": "bad_request", "detail": "missing prompt"])
        return
    }
    guard let schemaSpec = req["schema"] as? [String: Any] else {
        emitLine(["id": id, "ok": false, "error": "bad_request", "detail": "missing schema"])
        return
    }
    if case .unavailable = llmModel.availability {
        let info = availabilityInfo()
        emitLine(["id": id, "ok": false, "error": "unavailable",
                  "detail": info["reason"] as? String ?? "unavailable"])
        return
    }
    do {
        let root = try buildSchema(schemaSpec, fallbackName: "Result")
        let schema = try GenerationSchema(root: root, dependencies: [])
        let instructions = req["instructions"] as? String ?? ""
        let session = instructions.isEmpty
            ? LanguageModelSession(model: llmModel)
            : LanguageModelSession(model: llmModel, instructions: instructions)
        var options = GenerationOptions(sampling: .greedy)
        if let t = req["temperature"] as? Double { options.temperature = t }
        if let m = req["max_tokens"] as? Int { options.maximumResponseTokens = m }
        let resp = try await session.respond(to: prompt, schema: schema, options: options)
        let json = resp.content.jsonString
        guard let jdata = json.data(using: .utf8),
              let result = try? JSONSerialization.jsonObject(with: jdata) else {
            emitLine(["id": id, "ok": false, "error": "decode_failure",
                      "detail": "model output was not valid JSON"])
            return
        }
        emitLine(["id": id, "ok": true, "result": result])
    } catch let e as LanguageModelSession.GenerationError {
        emitLine(["id": id, "ok": false, "error": errorCode(for: e),
                  "detail": String(describing: e).prefix(300).description])
    } catch let e as SchemaError {
        emitLine(["id": id, "ok": false, "error": "bad_schema", "detail": e.description])
    } catch {
        emitLine(["id": id, "ok": false, "error": "generation_error",
                  "detail": String(describing: error).prefix(300).description])
    }
}

@main
struct AppleLLM {
    static func main() async {
        let mode = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "serve"
        if mode == "check" {
            emitLine(availabilityInfo())
            return
        }
        if case .available = llmModel.availability {
            LanguageModelSession(model: llmModel).prewarm()
        }
        // Requests run CONCURRENTLY — each line gets its own task and
        // session, so callers can fan out (e.g. the map phase of a long
        // summary). Responses interleave; callers match them by id.
        await withTaskGroup(of: Void.self) { group in
            while let line = readLine(strippingNewline: true) {
                if line.isEmpty { continue }
                group.addTask { await handle(line) }
            }
            await group.waitForAll()
        }
    }
}
