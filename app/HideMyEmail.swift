// Hide My Email — a small Mac app for managing iCloud+ Hide My Email addresses.
// The iCloud work is done by hme_helper.py (pyicloud) running as a child process.

import AppKit
import SwiftUI

// MARK: - Model

struct Alias: Identifiable, Hashable {
    let id: String
    let address: String
    var label: String
    var note: String
    var isActive: Bool
    let created: Date?
    let forwardTo: String

    init(_ d: [String: Any]) {
        id = d["anonymousId"] as? String ?? UUID().uuidString
        address = d["hme"] as? String ?? ""
        label = d["label"] as? String ?? ""
        note = d["note"] as? String ?? ""
        isActive = d["isActive"] as? Bool ?? true
        if let ms = d["createTimestamp"] as? Double {
            created = Date(timeIntervalSince1970: ms / 1000)
        } else {
            created = nil
        }
        forwardTo = d["forwardToEmail"] as? String ?? ""
    }
}

// MARK: - Helper process

struct HelperFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

final class Helper {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private var buffer = Data()
    private var nextId = 1
    private var pending: [Int: CheckedContinuation<Any, Error>] = [:]
    private let lock = NSLock()

    static let python = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/share/hme/venv/bin/python")

    init() throws {
        guard FileManager.default.isExecutableFile(atPath: Helper.python.path) else {
            throw HelperFailure(message: "Python environment not found at \(Helper.python.path). Run install.sh first.")
        }
        guard let script = Bundle.main.url(forResource: "hme_helper", withExtension: "py") else {
            throw HelperFailure(message: "hme_helper.py is missing from the app bundle.")
        }
        process.executableURL = Helper.python
        process.arguments = [script.path]
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.receive(handle.availableData)
        }
        process.terminationHandler = { [weak self] _ in
            self?.failAll("The background helper stopped unexpectedly.")
        }
        try process.run()
    }

    deinit { process.terminate() }

    func call(_ cmd: String, _ params: [String: Any] = [:]) async throws -> Any {
        try await withCheckedThrowingContinuation { cont in
            lock.lock()
            let id = nextId
            nextId += 1
            pending[id] = cont
            lock.unlock()
            var req = params
            req["id"] = id
            req["cmd"] = cmd
            do {
                var data = try JSONSerialization.data(withJSONObject: req)
                data.append(0x0A)
                try input.fileHandleForWriting.write(contentsOf: data)
            } catch {
                lock.lock()
                pending[id] = nil
                lock.unlock()
                cont.resume(throwing: error)
            }
        }
    }

    private func receive(_ data: Data) {
        if data.isEmpty { return }
        lock.lock()
        buffer.append(data)
        var lines: [Data] = []
        while let nl = buffer.firstIndex(of: 0x0A) {
            lines.append(buffer[buffer.startIndex..<nl])
            buffer.removeSubrange(buffer.startIndex...nl)
        }
        lock.unlock()
        for line in lines { handle(line) }
    }

    private func handle(_ line: Data) {
        guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              let id = obj["id"] as? Int else { return }
        lock.lock()
        let cont = pending.removeValue(forKey: id)
        lock.unlock()
        if obj["ok"] as? Bool == true {
            cont?.resume(returning: obj["result"] ?? NSNull())
        } else {
            cont?.resume(throwing: HelperFailure(message: obj["error"] as? String ?? "Unknown error"))
        }
    }

    private func failAll(_ message: String) {
        lock.lock()
        let all = pending
        pending = [:]
        lock.unlock()
        for cont in all.values { cont.resume(throwing: HelperFailure(message: message)) }
    }
}

// MARK: - App state

enum Phase { case starting, login, twoFactor, securityKey, ready, fatal(String) }

enum Filter: String, CaseIterable, Identifiable {
    case active = "Active", inactive = "Inactive", all = "All"
    var id: String { rawValue }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var phase: Phase = .starting
    @Published var appleId = ""
    @Published var aliases: [Alias] = []
    @Published var busy = false
    @Published var errorMessage: String?
    @Published var search = ""
    @Published var filter: Filter = .active
    @Published var selection: Alias.ID?
    @Published var toast: String?

    private var helper: Helper?

    var visible: [Alias] {
        aliases
            .filter {
                switch filter {
                case .active: return $0.isActive
                case .inactive: return !$0.isActive
                case .all: return true
                }
            }
            .filter {
                search.isEmpty
                    || $0.address.localizedCaseInsensitiveContains(search)
                    || $0.label.localizedCaseInsensitiveContains(search)
                    || $0.note.localizedCaseInsensitiveContains(search)
            }
            .sorted { ($0.created ?? .distantPast) > ($1.created ?? .distantPast) }
    }

    var selected: Alias? { aliases.first { $0.id == selection } }

    func start() async {
        do {
            helper = try Helper()
            let status = try await helper!.call("status") as? [String: Any] ?? [:]
            appleId = status["apple_id"] as? String ?? ""
            if status["has_password"] as? Bool == true {
                await login(password: nil)
            } else {
                phase = .login
            }
        } catch {
            phase = .fatal(error.localizedDescription)
        }
    }

    func login(password: String?) async {
        var params: [String: Any] = ["apple_id": appleId]
        if let password { params["password"] = password }
        await run {
            let result = try await self.helper!.call("login", params) as? [String: Any]
            switch result?["state"] as? String {
            case "2fa": self.phase = .twoFactor
            case "security_key": self.phase = .securityKey
            default: try await self.loggedIn()
            }
        } onError: {
            self.phase = .login
        }
    }

    func verify(code: String? = nil) async {
        var params: [String: Any] = [:]
        if let code { params["code"] = code } else { params["security_key"] = true }
        await run {
            _ = try await self.helper!.call("verify", params)
            try await self.loggedIn()
        }
    }

    private func loggedIn() async throws {
        phase = .ready
        try await reloadThrowing()
    }

    func logout() async {
        await run {
            _ = try await self.helper!.call("logout")
            self.aliases = []
            self.phase = .login
        }
    }

    func reload() async { await run { try await self.reloadThrowing() } }

    private func reloadThrowing() async throws {
        let list = try await helper!.call("list") as? [[String: Any]] ?? []
        aliases = list.map(Alias.init)
    }

    func generate() async -> String? {
        var email: String?
        await run {
            let r = try await self.helper!.call("generate") as? [String: Any]
            email = r?["hme"] as? String
        }
        return email
    }

    func create(address: String, label: String, note: String) async -> Bool {
        var ok = false
        await run {
            _ = try await self.helper!.call("reserve", ["hme": address, "label": label, "note": note])
            try await self.reloadThrowing()
            self.copy(address)
            self.selection = self.aliases.first { $0.address == address }?.id
            ok = true
        }
        return ok
    }

    func save(_ alias: Alias, label: String, note: String) async {
        await run {
            _ = try await self.helper!.call("update", ["id": alias.id, "label": label, "note": note])
            try await self.reloadThrowing()
            self.flash("Saved")
        }
    }

    func setActive(_ alias: Alias, _ active: Bool) async {
        await run {
            _ = try await self.helper!.call(active ? "reactivate" : "deactivate", ["id": alias.id])
            try await self.reloadThrowing()
        } onError: {
            Task { try? await self.reloadThrowing() }
        }
    }

    func delete(_ alias: Alias) async {
        await run {
            _ = try await self.helper!.call("delete", ["id": alias.id, "active": alias.isActive])
            self.selection = nil
            try await self.reloadThrowing()
        } onError: {
            // Deactivation may have succeeded even though deleting failed.
            Task { try? await self.reloadThrowing() }
        }
    }

    func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        flash("Copied \(text)")
    }

    func exportCSV() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "Hide My Email.csv"
        panel.allowedContentTypes = [.commaSeparatedText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        func esc(_ s: String) -> String { "\"" + s.replacingOccurrences(of: "\"", with: "\"\"") + "\"" }
        let df = ISO8601DateFormatter()
        var csv = "address,label,note,active,created,forward_to\n"
        for a in aliases {
            csv += [a.address, a.label, a.note, a.isActive ? "yes" : "no",
                    a.created.map(df.string) ?? "", a.forwardTo].map(esc).joined(separator: ",") + "\n"
        }
        do {
            try csv.write(to: url, atomically: true, encoding: .utf8)
            flash("Exported \(aliases.count) addresses")
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func flash(_ message: String) {
        toast = message
        Task {
            try? await Task.sleep(for: .seconds(2))
            if toast == message { toast = nil }
        }
    }

    private func run(_ work: @escaping () async throws -> Void, onError: (() -> Void)? = nil) async {
        busy = true
        defer { busy = false }
        do {
            try await work()
        } catch {
            errorMessage = error.localizedDescription
            onError?()
        }
    }
}

// MARK: - Views

struct ContentView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Group {
            switch model.phase {
            case .starting:
                ProgressView("Connecting to iCloud…").frame(maxWidth: .infinity, maxHeight: .infinity)
            case .login:
                LoginView()
            case .twoFactor:
                TwoFactorView()
            case .securityKey:
                SecurityKeyView()
            case .ready:
                MainView()
            case .fatal(let message):
                ContentUnavailableView("Can't start", systemImage: "exclamationmark.triangle", description: Text(message))
            }
        }
        .alert("Something went wrong", isPresented: Binding(
            get: { model.errorMessage != nil },
            set: { if !$0 { model.errorMessage = nil } }
        )) {
            Button("OK") { model.errorMessage = nil }
        } message: {
            Text(model.errorMessage ?? "")
        }
        .task { await model.start() }
    }
}

struct LoginView: View {
    @EnvironmentObject var model: AppModel
    @State private var password = ""

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "envelope.badge.shield.half.filled").font(.system(size: 48)).foregroundStyle(.tint)
            Text("Sign in to iCloud").font(.title2.bold())
            Text("Your password is stored in your Mac's Keychain.")
                .foregroundStyle(.secondary)
            Form {
                TextField("Apple ID", text: $model.appleId).textContentType(.username)
                SecureField("Password", text: $password).textContentType(.password)
            }
            .frame(width: 320)
            Button("Sign In") { Task { await model.login(password: password) } }
                .keyboardShortcut(.defaultAction)
                .disabled(model.appleId.isEmpty || password.isEmpty || model.busy)
            if model.busy { ProgressView().controlSize(.small) }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct TwoFactorView: View {
    @EnvironmentObject var model: AppModel
    @State private var code = ""

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "lock.iphone").font(.system(size: 48)).foregroundStyle(.tint)
            Text("Enter verification code").font(.title2.bold())
            Text("Apple sent a 6-digit code to your iPhone or other Apple devices.")
                .foregroundStyle(.secondary)
            TextField("123456", text: $code)
                .font(.system(.title, design: .monospaced))
                .multilineTextAlignment(.center)
                .frame(width: 180)
                .onSubmit(submit)
            Button("Verify", action: submit)
                .keyboardShortcut(.defaultAction)
                .disabled(code.count < 6 || model.busy)
            if model.busy { ProgressView().controlSize(.small) }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func submit() { Task { await model.verify(code: code) } }
}

struct SecurityKeyView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "key.horizontal").font(.system(size: 48)).foregroundStyle(.tint)
            Text("Use your security key").font(.title2.bold())
            Button("Continue, then touch the key") { Task { await model.verify() } }
                .disabled(model.busy)
            if model.busy { ProgressView().controlSize(.small) }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct MainView: View {
    @EnvironmentObject var model: AppModel
    @State private var showingNew = false

    var body: some View {
        NavigationSplitView {
            List(model.visible, selection: $model.selection) { alias in
                AliasRow(alias: alias)
                    .contextMenu {
                        Button("Copy Address") { model.copy(alias.address) }
                    }
            }
            .overlay {
                if model.visible.isEmpty && !model.busy {
                    ContentUnavailableView.search(text: model.search)
                }
            }
            .navigationSplitViewColumnWidth(min: 300, ideal: 360)
            .safeAreaInset(edge: .bottom) {
                Text("\(model.visible.count) of \(model.aliases.count) addresses")
                    .font(.caption).foregroundStyle(.secondary).padding(6)
            }
        } detail: {
            if let alias = model.selected {
                AliasDetail(alias: alias).id(alias.id)
            } else {
                ContentUnavailableView("No address selected", systemImage: "envelope",
                                       description: Text("Pick an address, or press ⌘N to create one."))
            }
        }
        .searchable(text: $model.search, placement: .sidebar, prompt: "Search address, label, note")
        .toolbar {
            ToolbarItem(placement: .navigation) {
                Picker("Show", selection: $model.filter) {
                    ForEach(Filter.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
            }
            ToolbarItemGroup {
                if model.busy { ProgressView().controlSize(.small) }
                Button { Task { await model.reload() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                    .keyboardShortcut("r")
                Button { showingNew = true } label: { Label("New Address", systemImage: "plus") }
                    .keyboardShortcut("n")
                Menu {
                    Button("Export to CSV…") { model.exportCSV() }
                    Divider()
                    Button("Sign Out \(model.appleId)") { Task { await model.logout() } }
                } label: { Label("More", systemImage: "ellipsis.circle") }
            }
        }
        .sheet(isPresented: $showingNew) { NewAliasSheet() }
        .overlay(alignment: .bottom) {
            if let toast = model.toast {
                Text(toast)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(.regularMaterial, in: Capsule())
                    .padding(.bottom, 20)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeInOut, value: model.toast)
    }
}

struct AliasRow: View {
    let alias: Alias

    var body: some View {
        HStack(spacing: 10) {
            Circle().fill(alias.isActive ? Color.green : Color.secondary.opacity(0.4)).frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(alias.label.isEmpty ? "No label" : alias.label)
                    .fontWeight(.medium)
                    .foregroundStyle(alias.label.isEmpty ? .secondary : .primary)
                Text(alias.address).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .padding(.vertical, 2)
    }
}

struct AliasDetail: View {
    @EnvironmentObject var model: AppModel
    let alias: Alias
    @State private var label = ""
    @State private var note = ""
    @State private var confirmDelete = false

    private var dirty: Bool { label != alias.label || note != alias.note }

    var body: some View {
        Form {
            Section {
                HStack {
                    Text(alias.address).font(.title3.monospaced()).textSelection(.enabled)
                    Spacer()
                    Button { model.copy(alias.address) } label: { Label("Copy", systemImage: "doc.on.doc") }
                        .keyboardShortcut("c", modifiers: [.command, .shift])
                }
                LabeledContent("Status", value: alias.isActive ? "Active — forwarding mail" : "Inactive — not forwarding")
                if !alias.forwardTo.isEmpty { LabeledContent("Forwards to", value: alias.forwardTo) }
                if let created = alias.created {
                    LabeledContent("Created", value: created.formatted(date: .long, time: .shortened))
                }
            }
            Section("Details") {
                TextField("Label", text: $label)
                TextField("Note", text: $note, axis: .vertical).lineLimit(3...8)
                HStack {
                    Spacer()
                    Button("Revert") { label = alias.label; note = alias.note }.disabled(!dirty)
                    Button("Save") { Task { await model.save(alias, label: label, note: note) } }
                        .keyboardShortcut("s")
                        .disabled(!dirty || model.busy)
                }
            }
            Section {
                HStack {
                    if alias.isActive {
                        Button("Deactivate") { Task { await model.setActive(alias, false) } }
                    } else {
                        Button("Reactivate") { Task { await model.setActive(alias, true) } }
                    }
                    Spacer()
                    Button("Delete…", role: .destructive) { confirmDelete = true }
                }
                .disabled(model.busy)
            } footer: {
                Text("Deactivating stops forwarding but keeps the address. Deleting removes it for good.")
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .onAppear { label = alias.label; note = alias.note }
        .confirmationDialog("Delete \(alias.address)?", isPresented: $confirmDelete) {
            Button("Delete Permanently", role: .destructive) { Task { await model.delete(alias) } }
        } message: {
            Text("Mail sent to this address will bounce. This cannot be undone.")
        }
    }
}

struct NewAliasSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var address: String?
    @State private var label = ""
    @State private var note = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("New Hide My Email address").font(.title3.bold())
            HStack {
                Text(address ?? "Generating…")
                    .font(.body.monospaced())
                    .foregroundStyle(address == nil ? .secondary : .primary)
                Spacer()
                Button { Task { await regenerate() } } label: { Image(systemName: "arrow.triangle.2.circlepath") }
                    .help("Try another address")
                    .disabled(model.busy)
            }
            .padding(10)
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            TextField("Label (e.g. Netflix)", text: $label)
            TextField("Note (optional)", text: $note, axis: .vertical).lineLimit(2...4)
            HStack {
                if model.busy { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Create & Copy") {
                    Task {
                        if let address, await model.create(address: address, label: label, note: note) {
                            dismiss()
                        }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(address == nil || label.isEmpty || model.busy)
            }
        }
        .textFieldStyle(.roundedBorder)
        .padding(20)
        .frame(width: 440)
        .task { await regenerate() }
    }

    private func regenerate() async {
        address = nil
        address = await model.generate()
        if address == nil { dismiss() }
    }
}

// MARK: - App

@main
struct HideMyEmailApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        Window("Hide My Email", id: "main") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 760, minHeight: 480)
        }
        .windowToolbarStyle(.unified)
    }
}
