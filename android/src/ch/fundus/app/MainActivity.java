package ch.fundus.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.content.Context;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.BufferedReader;
import java.io.InputStreamReader;

/**
 * Hülle für fundus auf dem Mac, erreichbar nur über Tailscale (tailnet only, HTTPS).
 * Die ganze Oberfläche kommt vom Mac – Änderungen dort brauchen keine neue APK.
 * Die App macht nur, was eine Webseite nicht kann: Inserate in der passenden App öffnen (Ricardo, Kleinanzeigen …),
 * fundus-Links aus Benachrichtigungen übernehmen, Zurück-Taste, Hinweis wenn der Mac nicht erreichbar ist.
 * Adresse steht in assets/adresse.txt (build-android.sh).
 */
public class MainActivity extends Activity {
    private WebView web;
    private String adresse = "";
    private boolean fehlerSeite = false;

    @Override
    protected void onCreate(Bundle zustand) {
        super.onCreate(zustand);
        adresseLesen();
        leistenFaerben(Color.WHITE);   // bis die Seite ihre Farbe meldet

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);   // Seiten schickt fundus mit no-cache, Bilder dürfen aus dem Cache
        s.setUserAgentString(s.getUserAgentString() + " FundusApp/1");  // schaltet auf dem Mac die App-Ansicht ein
        web.setBackgroundColor(Color.WHITE);
        web.addJavascriptInterface(new Bruecke(), "FundusAndroid");  // nur eigene Seiten laden hier (siehe Client)
        web.setWebViewClient(new Client());
        setContentView(web);
        laden(startAdresse(getIntent()));

        // Meldungen nach jedem Scan (Melder): einmalig um Erlaubnis fragen, Wecker stellen
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS") != 0) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1);
        }
        Melder.starten(this);
    }

    /** Alles ausserhalb von fundus (Inserate, Shops) öffnet die passende App bzw. den Browser. */
    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
            Uri ziel = r.getUrl();
            if (eigeneSeite(ziel)) return false;
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, ziel).addCategory(Intent.CATEGORY_BROWSABLE));
            } catch (ActivityNotFoundException e) {
                // keine App für diese Adresse – nichts tun
            }
            return true;
        }

        @Override
        public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
            if (r.isForMainFrame()) nichtErreichbar();
        }
    }

    private boolean eigeneSeite(Uri ziel) {
        return !adresse.isEmpty() && ziel.toString().startsWith(adresse);
    }

    private String startAdresse(Intent intent) {
        Uri daten = intent != null ? intent.getData() : null;
        return daten != null && eigeneSeite(daten) ? daten.toString() : adresse + "/";
    }

    private void laden(String url) {
        fehlerSeite = false;
        web.setWebViewClient(new Client());
        web.loadUrl(url);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (intent.getData() != null) laden(startAdresse(intent));  // fundus-Link aus einer Meldung
    }

    private void nichtErreichbar() {
        fehlerSeite = true;
        String html = "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
            + "<body style='font:17px system-ui,sans-serif;text-align:center;padding:90px 24px;margin:0;"
            + "background:#fff;color:#1c1c1a'>"
            + "<div style='font-size:30px;font-weight:800;letter-spacing:-.03em'>fundus</div>"
            + "<p style='color:#8e8b83'>Mac nicht erreichbar.<br>Ist Tailscale eingeschaltet und der Mac wach?</p>"
            + "<a href='fundus://neu' style='display:inline-block;margin-top:12px;padding:13px 26px;border-radius:10px;"
            + "background:#2f5d50;color:#fff;text-decoration:none;font-weight:600'>Neu laden</a></body>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                if ("fundus".equals(r.getUrl().getScheme())) laden(adresse + "/");
                return true;
            }
        });
    }

    @Override
    public void onBackPressed() {
        if (web != null && !fehlerSeite && web.canGoBack()) web.goBack(); else super.onBackPressed();
    }

    private void adresseLesen() {
        adresse = adresse(this);
    }

    /** Adresse von fundus auf dem Mac (assets/adresse.txt), ohne Schrägstrich am Ende; "" wenn sie fehlt. */
    static String adresse(Context ctx) {
        try (BufferedReader r = new BufferedReader(new InputStreamReader(ctx.getAssets().open("adresse.txt"), "UTF-8"))) {
            return r.readLine().trim().replaceAll("/+$", "");
        } catch (Exception e) {
            return "";
        }
    }

    /** Die Seite meldet ihre Hintergrundfarbe – Status- und Navigationsleiste übernehmen sie (wie bei anderen Apps). */
    private class Bruecke {
        @JavascriptInterface
        public void leisten(String cssFarbe) {
            int farbe = cssFarbeLesen(cssFarbe);
            runOnUiThread(() -> leistenFaerben(farbe));
        }

        /** Scan von Hand gestartet: in ein paar Minuten nachfragen, dann bis alles ausgewertet ist (Melder). */
        @JavascriptInterface
        public void nachScan() {
            Melder.wecken(getApplicationContext(), System.currentTimeMillis() + 4 * 60_000L);
        }
    }

    static int cssFarbeLesen(String css) {
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("(\\d+)\\D+(\\d+)\\D+(\\d+)").matcher(css == null ? "" : css);
        return m.find() ? Color.rgb(Integer.parseInt(m.group(1)), Integer.parseInt(m.group(2)), Integer.parseInt(m.group(3))) : Color.WHITE;
    }

    private void leistenFaerben(int farbe) {
        boolean hell = Color.luminance(farbe) > 0.5f;
        getWindow().setBackgroundDrawable(new ColorDrawable(farbe));
        getWindow().setStatusBarColor(farbe);
        getWindow().setNavigationBarColor(farbe);
        if (Build.VERSION.SDK_INT >= 29) {   // kein grauer Schleier über den Leisten
            getWindow().setStatusBarContrastEnforced(false);
            getWindow().setNavigationBarContrastEnforced(false);
        }
        if (Build.VERSION.SDK_INT >= 28) getWindow().setNavigationBarDividerColor(farbe);
        if (web != null) web.setBackgroundColor(farbe);
        getWindow().getDecorView().setSystemUiVisibility(hell
            ? View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR : 0);
    }
}
