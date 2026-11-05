// fundus als eigene Mac-App: ein Fenster mit der Weboberfläche (http://localhost:8100), Symbol im Dock.
// Der Server läuft weiter als LaunchAgent «ch.fundus.serve» – die App öffnet nur das Fenster.
// Bauen: «fundus einrichten.command».

import Cocoa
import WebKit

enum Konfig {
    static let port = Bundle.main.object(forInfoDictionaryKey: "FundusPort") as? String ?? "8100"
    static let basis = URL(string: "http://localhost:\(port)")!
    static let dienst = "gui/\(getuid())/ch.fundus.serve"
}

// Server nicht erreichbar (z.B. nach Absturz): LaunchAgent anstossen, höchstens alle 30 s
final class Dienst {
    private var zuletzt = Date.distantPast

    func anstossen() {
        guard Date().timeIntervalSince(zuletzt) > 30 else { return }
        zuletzt = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", Konfig.dienst]
        try? p.run()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    var fenster: NSWindow!
    var web: WKWebView!
    let dienst = Dienst()

    func applicationDidFinishLaunching(_ n: Notification) {
        hauptmenue()
        fensterBauen()
        zeigen()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool { zeigen(); return true }

    // MARK: Fenster

    func fensterBauen() {
        let konf = WKWebViewConfiguration()
        konf.applicationNameForUserAgent = "FundusApp/1.0"
        web = WKWebView(frame: .zero, configuration: konf)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = true
        fenster = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 860),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                           backing: .buffered, defer: false)
        fenster.title = "fundus"
        fenster.titlebarAppearsTransparent = false
        fenster.minSize = NSSize(width: 420, height: 400)
        fenster.isReleasedWhenClosed = false
        fenster.contentView = web
        fenster.delegate = self
        fenster.center()
        fenster.setFrameAutosaveName("FundusHauptfenster")
        laden()
    }

    func laden(_ pfad: String = "") { web.load(URLRequest(url: Konfig.basis.appendingPathComponent(pfad))) }

    @objc func zeigen() {
        NSApp.activate(ignoringOtherApps: true)
        fenster.makeKeyAndOrderFront(nil)
    }

    // Seite nicht erreichbar (Server startet gerade): Dienst anstossen, kurz warten, nochmals laden
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        let err = e as NSError
        guard err.domain == NSURLErrorDomain, err.code != NSURLErrorCancelled else { return }  // z.B. Link im Browser geöffnet
        var url = err.userInfo[NSURLErrorFailingURLErrorKey] as? URL ?? Konfig.basis
        if url.host != Konfig.basis.host { url = Konfig.basis }
        dienst.anstossen()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.web.load(URLRequest(url: url)) }
    }

    // Inserate und andere Links nach aussen im normalen Browser öffnen
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = a.request.url, url.host != "localhost", url.host != "127.0.0.1", url.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(url); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let url = a.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // confirm()/alert() der Weboberfläche (z.B. «Suche samt Treffern löschen?»)
    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = m
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Abbrechen")
        a.beginSheetModal(for: fenster) { completionHandler($0 == .alertFirstButtonReturn) }
    }
    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = m
        a.beginSheetModal(for: fenster) { _ in completionHandler() }
    }

    // MARK: Menüs

    func hauptmenue() {
        let haupt = NSMenu()
        let app = NSMenu()
        app.addItem(withTitle: "Über fundus", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Einstellungen …", action: #selector(einstellungen), keyEquivalent: ",")
        app.addItem(.separator())
        app.addItem(withTitle: "fundus ausblenden", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "fundus beenden", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        haupt.addItem(withTitle: "fundus", action: nil, keyEquivalent: "").submenu = app

        let bearb = NSMenu(title: "Bearbeiten")
        bearb.addItem(withTitle: "Widerrufen", action: Selector(("undo:")), keyEquivalent: "z")
        bearb.addItem(.separator())
        bearb.addItem(withTitle: "Ausschneiden", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        bearb.addItem(withTitle: "Kopieren", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        bearb.addItem(withTitle: "Einsetzen", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        bearb.addItem(withTitle: "Alles auswählen", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        haupt.addItem(withTitle: "Bearbeiten", action: nil, keyEquivalent: "").submenu = bearb

        let ansicht = NSMenu(title: "Darstellung")
        ansicht.addItem(withTitle: "Übersicht", action: #selector(uebersicht), keyEquivalent: "1")
        ansicht.addItem(withTitle: "Zurück", action: #selector(zurueck), keyEquivalent: "[")
        ansicht.addItem(withTitle: "Vorwärts", action: #selector(vorwaerts), keyEquivalent: "]")
        ansicht.addItem(.separator())
        ansicht.addItem(withTitle: "Neu laden", action: #selector(neuLaden), keyEquivalent: "r")
        ansicht.addItem(withTitle: "Im Browser öffnen", action: #selector(imBrowser), keyEquivalent: "b")
        haupt.addItem(withTitle: "Darstellung", action: nil, keyEquivalent: "").submenu = ansicht

        let fen = NSMenu(title: "Fenster")
        fen.addItem(withTitle: "Minimieren", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        fen.addItem(withTitle: "Schliessen", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fen.addItem(withTitle: "fundus", action: #selector(zeigen), keyEquivalent: "0")
        haupt.addItem(withTitle: "Fenster", action: nil, keyEquivalent: "").submenu = fen
        NSApp.windowsMenu = fen

        NSApp.mainMenu = haupt
    }

    @objc func uebersicht() { zeigen(); laden() }
    @objc func einstellungen() { zeigen(); laden("settings") }
    @objc func zurueck() { web.goBack() }
    @objc func vorwaerts() { web.goForward() }
    @objc func neuLaden() { zeigen(); web.reload() }
    @objc func imBrowser() { NSWorkspace.shared.open(web.url ?? Konfig.basis) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
