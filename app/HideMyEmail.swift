// Hide My Email — a small Mac app for managing iCloud+ Hide My Email addresses.
// The iCloud work is done by hme_helper.py (pyicloud) running as a child process.

import AppKit
import Carbon.HIToolbox
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
    /// Where the address was made: "Safari — example.com", "Created by hand", or an app name.
    let origin: String

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

        let kind = d["origin"] as? String ?? ""
        let domain = d["domain"] as? String ?? ""
        let appName = d["originAppName"] as? String ?? ""
        var source: String
        switch kind {
        case "SAFARI": source = "Safari"
        case "ON_DEMAND": source = "Created by hand"
        case "": source = ""
        default: source = appName.isEmpty ? kind.capitalized : appName
        }
        if !domain.isEmpty { source = source.isEmpty ? domain : "\(source) — \(domain)" }
        origin = source
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
        // Downloaded builds carry a self-contained helper; developer builds run the script with Python.
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("hme-helper/hme-helper")
        if let bundled, FileManager.default.isExecutableFile(atPath: bundled.path) {
            process.executableURL = bundled
            process.arguments = []
        } else {
            guard FileManager.default.isExecutableFile(atPath: Helper.python.path) else {
                throw HelperFailure(message: "Python environment not found at \(Helper.python.path). Run install.sh first.")
            }
            guard let script = Bundle.main.url(forResource: "hme_helper", withExtension: "py") else {
                throw HelperFailure(message: "hme_helper.py is missing from the app bundle.")
            }
            process.executableURL = Helper.python
            process.arguments = [script.path]
        }
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

    deinit { stop() }

    /// End the helper process (used when the app quits, so nothing is left running).
    func stop() {
        try? input.fileHandleForWriting.close()
        if process.isRunning { process.terminate() }
    }

    func call(_ cmd: String, _ params: [String: Any] = [:]) async throws -> Any {
        // "id" and "cmd" belong to the protocol; an address id goes in "alias_id".
        precondition(params["id"] == nil && params["cmd"] == nil, "reserved key in params")
        return try await withCheckedThrowingContinuation { cont in
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

enum Phase {
    case starting, login, twoFactor, securityKey, ready, fatal(String)
    var isReady: Bool { if case .ready = self { return true } else { return false } }
}

enum Filter: String, CaseIterable, Identifiable {
    case active = "Active", inactive = "Inactive", all = "All"
    var id: String { rawValue }
}

enum SortOrder: String, CaseIterable, Identifiable {
    case newest = "Newest First", oldest = "Oldest First", nameAZ = "Name A–Z", nameZA = "Name Z–A"
    var id: String { rawValue }

    func inOrder(_ a: Alias, _ b: Alias) -> Bool {
        switch self {
        case .newest: return (a.created ?? .distantPast) > (b.created ?? .distantPast)
        case .oldest: return (a.created ?? .distantPast) < (b.created ?? .distantPast)
        case .nameAZ, .nameZA:
            // Sort by label; unlabelled addresses always go last, by address.
            if a.label.isEmpty != b.label.isEmpty { return b.label.isEmpty }
            if a.label.isEmpty { return a.address < b.address }
            let order = a.label.localizedStandardCompare(b.label)
            return self == .nameAZ ? order == .orderedAscending : order == .orderedDescending
        }
    }
}

@MainActor
final class AppModel: ObservableObject {
    /// Shared by the main window, the menu bar extra and the hotkey panel.
    static let shared = AppModel()

    @Published var phase: Phase = .starting
    @Published var appleId = ""
    @Published var aliases: [Alias] = []
    @Published var busy = false
    @Published var errorMessage: String?
    @Published var search = ""
    @Published var filter: Filter = .active
    @Published var sort: SortOrder = SortOrder(rawValue: UserDefaults.standard.string(forKey: "sort") ?? "") ?? .newest {
        didSet { UserDefaults.standard.set(sort.rawValue, forKey: "sort") }
    }
    @Published var selection: Set<Alias.ID> = []
    @Published var toast: String?

    private var helper: Helper?

    func shutdown() { helper?.stop() }
    private var started = false

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
                    || $0.origin.localizedCaseInsensitiveContains(search)
            }
            .sorted(by: sort.inOrder)
    }

    /// Selected addresses, in list order.
    var selectedAliases: [Alias] { visible.filter { selection.contains($0.id) } }

    /// The selected address when exactly one is selected.
    var selected: Alias? { selection.count == 1 ? aliases.first { selection.contains($0.id) } : nil }

    func start() async {
        // The window, menu bar panel and hotkey panel all call this; connect once.
        guard !started else { return }
        started = true
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
            self.selection = Set(self.aliases.filter { $0.address == address }.map(\.id))
            ok = true
        }
        return ok
    }

    /// Generate, reserve and copy a new address in one go (menu bar and hotkey panel).
    /// Throws instead of showing the main window's alert, which may not be open.
    func quickCreate(label: String) async throws -> String {
        guard phase.isReady, let helper else {
            throw HelperFailure(message: "Open Hide My Email and sign in first.")
        }
        let generated = try await helper.call("generate") as? [String: Any]
        guard let address = generated?["hme"] as? String else {
            throw HelperFailure(message: "iCloud did not generate an address.")
        }
        _ = try await helper.call("reserve", ["hme": address, "label": label, "note": ""])
        copy(address)
        try? await reloadThrowing()
        return address
    }

    func save(_ alias: Alias, label: String, note: String) async {
        await run {
            _ = try await self.helper!.call("update", ["alias_id": alias.id, "label": label, "note": note])
            try await self.reloadThrowing()
            self.flash("Saved")
        }
    }

    func setActive(_ targets: [Alias], _ active: Bool) async {
        let todo = targets.filter { $0.isActive != active }
        await batch(todo, verb: active ? "Reactivated" : "Deactivated") { alias in
            _ = try await self.helper!.call(active ? "reactivate" : "deactivate", ["alias_id": alias.id])
        }
    }

    func delete(_ targets: [Alias]) async {
        await batch(targets, verb: "Deleted") { alias in
            _ = try await self.helper!.call("delete", ["alias_id": alias.id, "active": alias.isActive])
            self.selection.remove(alias.id)
        }
    }

    /// Run one helper call per address, keep going past failures, then reload once.
    private func batch(_ targets: [Alias], verb: String, _ work: @escaping (Alias) async throws -> Void) async {
        guard !targets.isEmpty else { return }
        await run {
            var failures: [String] = []
            for alias in targets {
                do { try await work(alias) } catch { failures.append("\(alias.address): \(error.localizedDescription)") }
            }
            try await self.reloadThrowing()
            let done = targets.count - failures.count
            if done > 0 { self.flash("\(verb) \(done) address\(done == 1 ? "" : "es")") }
            if !failures.isEmpty {
                throw HelperFailure(message: failures.joined(separator: "\n\n"))
            }
        }
    }

    func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        flash(text.contains("\n") ? "Copied \(text.split(separator: "\n").count) addresses" : "Copied \(text)")
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
    @AppStorage("keepInMenuBar") private var keepInMenuBar = false
    @State private var showingNew = false
    @State private var pendingDelete: [Alias] = []

    var body: some View {
        NavigationSplitView {
            List(model.visible, selection: $model.selection) { alias in
                AliasRow(alias: alias)
                    .contextMenu { rowMenu(for: alias) }
            }
            // ⌘C copies the selected addresses; Delete asks what to do with them.
            .onCopyCommand {
                model.selectedAliases.isEmpty ? [] : [NSItemProvider(object: model.selectedAliases.map(\.address).joined(separator: "\n") as NSString)]
            }
            .onDeleteCommand { pendingDelete = model.selectedAliases }
            .overlay {
                if model.visible.isEmpty && !model.busy {
                    ContentUnavailableView.search(text: model.search)
                }
            }
            .navigationSplitViewColumnWidth(min: 300, ideal: 360)
            .safeAreaInset(edge: .top, spacing: 0) {
                VStack(spacing: 0) {
                    HStack {
                        Spacer()
                        Menu {
                            Picker("Sort By", selection: $model.sort) {
                                ForEach(SortOrder.allCases) { Text($0.rawValue).tag($0) }
                            }
                            .pickerStyle(.inline)
                        } label: {
                            Label(model.sort.rawValue, systemImage: "arrow.up.arrow.down")
                                .font(.callout)
                        }
                        .menuStyle(.borderlessButton)
                        .fixedSize()
                        .help("Sort addresses")
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 5)
                    Divider()
                }
                .background(.bar)
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                VStack(spacing: 0) {
                    Divider()
                    Text(model.selection.count > 1
                         ? "\(model.selection.count) selected · \(model.visible.count) of \(model.aliases.count) addresses"
                         : "\(model.visible.count) of \(model.aliases.count) addresses")
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                }
                .background(.bar)
            }
        } detail: {
            if let alias = model.selected {
                AliasDetail(alias: alias).id(alias.id)
            } else if model.selection.count > 1 {
                BatchDetail(aliases: model.selectedAliases, onDelete: { pendingDelete = $0 })
            } else {
                ContentUnavailableView("No address selected", systemImage: "envelope",
                                       description: Text("Pick an address, or press ⌘N to create one.\n⌘-click or ⇧-click to select several."))
            }
        }
        .confirmationDialog(deleteTitle, isPresented: Binding(
            get: { !pendingDelete.isEmpty },
            set: { if !$0 { pendingDelete = [] } }
        )) {
            let targets = pendingDelete
            if targets.contains(where: \.isActive) {
                Button("Deactivate") { Task { await model.setActive(targets, false) } }
            }
            Button("Delete Permanently", role: .destructive) { Task { await model.delete(targets) } }
        } message: {
            Text("Deactivating stops forwarding but keeps the address. Deleting removes it for good: mail sent to it will bounce.")
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
                    Button("Check for Updates…") { Task { await Updater.shared.check(manual: true) } }
                    Toggle("Keep Running in Menu Bar When Closed", isOn: $keepInMenuBar)
                    Divider()
                    Button("Sign Out \(model.appleId)") { Task { await model.logout() } }
                } label: { Label("More", systemImage: "ellipsis.circle") }
            }
        }
        .sheet(isPresented: $showingNew) { NewAliasSheet() }
        .safeAreaInset(edge: .top, spacing: 0) { UpdateBanner() }
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

    private var deleteTitle: String {
        pendingDelete.count == 1 ? pendingDelete[0].address : "\(pendingDelete.count) addresses"
    }

    /// Right-click acts on the whole selection when the clicked row is part of it.
    @ViewBuilder private func rowMenu(for alias: Alias) -> some View {
        let targets = model.selection.contains(alias.id) ? model.selectedAliases : [alias]
        Button(targets.count == 1 ? "Copy Address" : "Copy \(targets.count) Addresses") {
            model.copy(targets.map(\.address).joined(separator: "\n"))
        }
        Divider()
        if targets.contains(where: \.isActive) {
            Button("Deactivate") { Task { await model.setActive(targets, false) } }
        }
        if targets.contains(where: { !$0.isActive }) {
            Button("Reactivate") { Task { await model.setActive(targets, true) } }
        }
        Button("Delete…", role: .destructive) { pendingDelete = targets }
    }
}

struct BatchDetail: View {
    @EnvironmentObject var model: AppModel
    let aliases: [Alias]
    let onDelete: ([Alias]) -> Void

    var body: some View {
        let active = aliases.filter(\.isActive).count
        VStack(spacing: 18) {
            Image(systemName: "square.stack.3d.up").font(.system(size: 44)).foregroundStyle(.tint)
            Text("\(aliases.count) addresses selected").font(.title2.bold())
            Text("\(active) active, \(aliases.count - active) inactive").foregroundStyle(.secondary)
            HStack {
                Button("Copy All") { model.copy(aliases.map(\.address).joined(separator: "\n")) }
                Button("Deactivate") { Task { await model.setActive(aliases, false) } }.disabled(active == 0)
                Button("Reactivate") { Task { await model.setActive(aliases, true) } }.disabled(active == aliases.count)
                Button("Delete…", role: .destructive) { onDelete(aliases) }
            }
            .disabled(model.busy)
            if model.busy { ProgressView().controlSize(.small) }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
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
                if !alias.origin.isEmpty { LabeledContent("Created from", value: alias.origin) }
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
                        Button("Deactivate") { Task { await model.setActive([alias], false) } }
                    } else {
                        Button("Reactivate") { Task { await model.setActive([alias], true) } }
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
            Button("Delete Permanently", role: .destructive) { Task { await model.delete([alias]) } }
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

// MARK: - Quick create (menu bar and global hotkey)

struct QuickCreateView: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject private var updater = Updater.shared
    @Environment(\.openWindow) private var openWindow
    var onDone: () -> Void = {}
    @State private var label = ""
    @State private var working = false
    @State private var created: String?
    @State private var error: String?
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("New Hide My Email address").font(.headline)
            TextField("What's it for? e.g. Netflix", text: $label)
                .textFieldStyle(.roundedBorder)
                .focused($focused)
                .onSubmit(create)
                .disabled(working)
            HStack {
                if working {
                    ProgressView().controlSize(.small)
                    Text("Creating…").foregroundStyle(.secondary)
                } else if let created {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    Text(created).font(.callout.monospaced()).textSelection(.enabled)
                    Text("copied").foregroundStyle(.secondary)
                } else if let error {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    Text(error).font(.callout).fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("Press Return to create it and copy it.").font(.callout).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 20)
            if let ready = updater.ready {
                HStack {
                    Image(systemName: "arrow.down.circle.fill").foregroundStyle(.tint)
                    Text("Version \(ready.version) is ready").font(.callout)
                    Spacer()
                    Button("Restart to Update") { updater.install() }.controlSize(.small)
                }
            }
            Divider()
            HStack {
                Button("Open Hide My Email") {
                    openWindow(id: "main")
                    NSApp.activate(ignoringOtherApps: true)
                    onDone()
                }
                Spacer()
                Text("⌃⌥⌘H").font(.caption).foregroundStyle(.secondary).help("Global shortcut for this panel")
                Button("Quit") { NSApp.terminate(nil) }
            }
            .buttonStyle(.borderless)
        }
        .padding(14)
        .frame(width: 340)
        .task { await model.start() }
        .onAppear { focused = true }
    }

    private func create() {
        let text = label.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty, !working else { return }
        working = true
        error = nil
        created = nil
        Task {
            do {
                created = try await model.quickCreate(label: text)
                label = ""
            } catch {
                self.error = error.localizedDescription
            }
            working = false
        }
    }
}

/// A floating panel with QuickCreateView, shown by the global hotkey from any app.
@MainActor
final class QuickPanel {
    static let shared = QuickPanel()
    private var panel: NSPanel?
    private var hotKey: EventHotKeyRef?

    func unregisterHotKey() {
        if let hotKey { UnregisterEventHotKey(hotKey) }
        hotKey = nil
        panel?.orderOut(nil)
    }

    func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            DispatchQueue.main.async { QuickPanel.shared.toggle() }
            return noErr
        }, 1, &spec, nil, nil)
        let id = EventHotKeyID(signature: OSType(0x484D_4531), id: 1)  // "HME1"
        RegisterEventHotKey(UInt32(kVK_ANSI_H), UInt32(controlKey | optionKey | cmdKey), id,
                            GetApplicationEventTarget(), 0, &hotKey)
    }

    func toggle() {
        if let panel, panel.isVisible { panel.orderOut(nil); return }
        let panel = self.panel ?? makePanel()
        self.panel = panel
        // Fresh view each time, so the field is empty and focused.
        panel.contentView = NSHostingView(rootView: QuickCreateView(onDone: { [weak panel] in panel?.orderOut(nil) })
            .environmentObject(AppModel.shared))
        panel.center()
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 170),
                            styleMask: [.titled, .closable, .fullSizeContentView],
                            backing: .buffered, defer: false)
        panel.title = "Hide My Email"
        panel.titlebarAppearsTransparent = true
        panel.level = .floating
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = true
        return panel
    }
}

// MARK: - Updates

/// Checks GitHub for a newer release, downloads its disk image in the background,
/// and on request swaps it in with Resources/update.sh and reopens the app.
@MainActor
final class Updater: ObservableObject {
    static let shared = Updater()
    static let releasesAPI = URL(string: "https://api.github.com/repos/decentquintessential/hidemyemail/releases/latest")!
    static let asset = "HideMyEmail-Mac.dmg"

    struct Ready: Equatable { let version: String; let dmg: URL }
    @Published var ready: Ready?
    @Published var dismissed = false
    private var timer: Timer?
    private var checking = false

    var current: String { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0" }

    /// Only downloaded builds update themselves; developer builds have no bundled helper.
    var enabled: Bool {
        let helper = Bundle.main.resourceURL?.appendingPathComponent("hme-helper/hme-helper").path ?? ""
        return FileManager.default.isExecutableFile(atPath: helper)
            && Bundle.main.url(forResource: "update", withExtension: "sh") != nil
    }

    func start() {
        guard enabled, timer == nil else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { Task { await self.check() } }
        timer = Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { _ in
            Task { @MainActor in await Updater.shared.check() }
        }
    }

    static func parse(_ version: String) -> [Int] {
        version.trimmingCharacters(in: CharacterSet(charactersIn: "vV ")).split(separator: ".").map { Int($0) ?? 0 }
    }

    static func isNewer(_ a: String, than b: String) -> Bool {
        let x = parse(a), y = parse(b)
        for i in 0..<max(x.count, y.count) {
            let l = i < x.count ? x[i] : 0, r = i < y.count ? y[i] : 0
            if l != r { return l > r }
        }
        return false
    }

    func check(manual: Bool = false) async {
        guard enabled else {
            if manual { alert("Updates aren't available for this build", "This copy wasn't downloaded from GitHub, so it can't update itself.") }
            return
        }
        if ready != nil { dismissed = false; return }
        guard !checking else { return }
        checking = true
        defer { checking = false }
        do {
            var request = URLRequest(url: Self.releasesAPI)
            request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
            request.setValue("HideMyEmail/\(current)", forHTTPHeaderField: "User-Agent")
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let release = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let tag = release["tag_name"] as? String else {
                throw HelperFailure(message: "GitHub didn't return the latest release.")
            }
            let version = tag.trimmingCharacters(in: CharacterSet(charactersIn: "vV"))
            let assets = release["assets"] as? [[String: Any]] ?? []
            guard Self.isNewer(version, than: current),
                  let asset = assets.first(where: { $0["name"] as? String == Self.asset }),
                  let link = asset["browser_download_url"] as? String, let url = URL(string: link) else {
                if manual { alert("You're up to date", "Hide My Email \(current) is the newest version.") }
                return
            }
            let dmg = try await download(url, version: version, size: asset["size"] as? Int ?? 0)
            ready = Ready(version: version, dmg: dmg)
            dismissed = false
        } catch {
            // Automatic checks stay quiet, e.g. when offline.
            if manual { alert("Couldn't check for updates", error.localizedDescription) }
        }
    }

    private func download(_ url: URL, version: String, size: Int) async throws -> URL {
        let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("HideMyEmail-update", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let target = folder.appendingPathComponent("HideMyEmail-\(version).dmg")
        if let existing = try? target.resourceValues(forKeys: [.fileSizeKey]).fileSize, existing == size, size > 0 {
            return target
        }
        let (temp, response) = try await URLSession.shared.download(from: url)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw HelperFailure(message: "Downloading the update failed.")
        }
        let got = (try? temp.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? -1
        if size > 0 && got != size { throw HelperFailure(message: "The downloaded update was incomplete.") }
        try? FileManager.default.removeItem(at: target)
        try FileManager.default.moveItem(at: temp, to: target)
        return target
    }

    /// Hand over to update.sh, which waits for this app to quit, replaces it and reopens it.
    func install() {
        guard let ready, let script = Bundle.main.url(forResource: "update", withExtension: "sh") else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script.path, String(ProcessInfo.processInfo.processIdentifier),
                             ready.dmg.path, Bundle.main.bundlePath]
        do {
            try process.run()
            NSApp.terminate(nil)
        } catch {
            alert("Couldn't install the update", error.localizedDescription)
        }
    }

    private func alert(_ title: String, _ text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}

struct UpdateBanner: View {
    @ObservedObject var updater = Updater.shared

    var body: some View {
        if let ready = updater.ready, !updater.dismissed {
            HStack(spacing: 12) {
                Image(systemName: "arrow.down.circle.fill").foregroundStyle(.tint)
                Text("Hide My Email \(ready.version) is ready to install (you have \(updater.current)).")
                Spacer()
                Button("Later") { updater.dismissed = true }.buttonStyle(.borderless)
                Button("Restart to Update") { updater.install() }.buttonStyle(.borderedProminent)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(.tint.opacity(0.12))
            .overlay(alignment: .bottom) { Divider() }
        }
    }
}

// MARK: - App

/// Closing the window quits the app completely, unless "Keep Running in Menu Bar" is on.
/// Quitting stops the helper process and releases the global shortcut.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        !UserDefaults.standard.bool(forKey: "keepInMenuBar")
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            QuickPanel.shared.unregisterHotKey()
            AppModel.shared.shutdown()
        }
    }
}

@main
struct HideMyEmailApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel.shared
    @AppStorage("keepInMenuBar") private var keepInMenuBar = false

    init() {
        QuickPanel.shared.registerHotKey()
        Updater.shared.start()
    }

    var body: some Scene {
        Window("Hide My Email", id: "main") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 760, minHeight: 480)
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(after: .appInfo) {
                Button("Check for Updates…") { Task { await Updater.shared.check(manual: true) } }
                Toggle("Keep Running in Menu Bar When Closed", isOn: $keepInMenuBar)
            }
        }

        MenuBarExtra("Hide My Email", systemImage: "envelope.badge.shield.half.filled") {
            QuickCreateView().environmentObject(model)
        }
        .menuBarExtraStyle(.window)
    }
}
