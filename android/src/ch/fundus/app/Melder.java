package ch.fundus.app;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Locale;
import java.util.TimeZone;

/**
 * Meldungen ohne fremden Dienst: kurz nach jedem geplanten Scan fragt die App den Mac
 * (/api/app/notices), ob Suche und KI-Auswertung fertig sind, und zeigt die neuen Top-Angebote an.
 * Wann der nächste Scan ist, sagt der Mac selbst – so fragt die App nur etwa zweimal am Tag.
 * Läuft der Scan noch oder ist der Mac nicht erreichbar (Tailscale aus), fragt sie später nochmals.
 */
public class Melder extends BroadcastReceiver {
    private static final String KANAL = "angebote";
    private static final String KANAL_DRINGEND = "auktion";   // Auktion endet gleich: mit Ton und Einblendung
    private static final long MINUTE = 60_000L;

    @Override
    public void onReceive(Context ctx, Intent intent) {
        final PendingResult ergebnis = goAsync();
        new Thread(() -> {
            try {
                abfragen(ctx.getApplicationContext());
            } finally {
                ergebnis.finish();
            }
        }).start();
    }

    /** Beim Öffnen der App: Stand holen und den nächsten Wecker stellen (im Hintergrund). */
    static void starten(Context ctx) {
        final Context app = ctx.getApplicationContext();
        new Thread(() -> abfragen(app)).start();
    }

    static void abfragen(Context ctx) {
        SharedPreferences prefs = ctx.getSharedPreferences("melder", Context.MODE_PRIVATE);
        long letzte = prefs.getLong("letzte", -1);
        String adresse = MainActivity.adresse(ctx);
        if (adresse.isEmpty()) return;
        try {
            JSONObject antwort = holen(adresse + "/api/app/notices?after=" + letzte);
            JSONArray meldungen = antwort.getJSONArray("notices");
            for (int i = 0; i < meldungen.length(); i++) {
                JSONObject m = meldungen.getJSONObject(i);
                zeigen(ctx, m.getInt("id"), m.getString("title"), m.getString("body"), m.optString("url", adresse + "/"),
                        m.optBoolean("urgent", false));
            }
            // Auktion endet bald: dieser Zeitpunkt ist zeitkritisch (exakter Wecker, rascher Neuversuch)
            long auktion = antwort.isNull("auction_check") ? 0 : zeitpunkt(antwort.optString("auction_check"));
            prefs.edit().putLong("letzte", antwort.getLong("last_id")).putLong("auktion", auktion).apply();
            long jetzt = System.currentTimeMillis();
            long naechste = antwort.getBoolean("busy")
                    ? jetzt + 15 * MINUTE                                   // Scan oder Auswertung läuft noch
                    : Math.max(zeitpunkt(antwort.getString("next_check")), jetzt + 5 * MINUTE);
            if (auktion > 0) naechste = Math.min(naechste, Math.max(auktion, jetzt + MINUTE));
            wecken(ctx, naechste, auktion > 0 && naechste <= auktion + MINUTE);
        } catch (Exception e) {
            // Mac nicht erreichbar: später nochmals – steht eine Auktions-Prüfung an oder ist sie eben vorbei, rasch
            long jetzt = System.currentTimeMillis();
            long auktion = prefs.getLong("auktion", 0);
            boolean eilig = auktion > 0 && auktion - 10 * MINUTE < jetzt && jetzt < auktion + 50 * MINUTE;
            wecken(ctx, jetzt + (eilig ? 3 : 30) * MINUTE, eilig);
        }
    }

    private static JSONObject holen(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(15000);
        c.setReadTimeout(20000);
        c.setRequestProperty("User-Agent", "FundusApp/1");
        try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"))) {
            StringBuilder sb = new StringBuilder();
            for (String z; (z = r.readLine()) != null; ) sb.append(z);
            return new JSONObject(sb.toString());
        } finally {
            c.disconnect();
        }
    }

    private static long zeitpunkt(String iso) throws Exception {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.ROOT);
        f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return f.parse(iso).getTime();
    }

    static void wecken(Context ctx, long wann) {
        wecken(ctx, wann, false);
    }

    /** genau = exakter Wecker: der ungenaue wird im Stromsparmodus um Stunden verschoben – für Auktionen zu spät. */
    static void wecken(Context ctx, long wann, boolean genau) {
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        PendingIntent pi = PendingIntent.getBroadcast(ctx, 0, new Intent(ctx, Melder.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        boolean darf = android.os.Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms();
        if (genau && darf) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, wann, pi);
        else am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, wann, pi);   // ungenau, aber auch im Stromsparmodus
    }

    private static void zeigen(Context ctx, int id, String titel, String text, String url, boolean dringend) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel(KANAL, "Neue Top-Angebote", NotificationManager.IMPORTANCE_DEFAULT));
        NotificationChannel alarm = new NotificationChannel(KANAL_DRINGEND, "Auktion endet gleich", NotificationManager.IMPORTANCE_HIGH);
        alarm.enableVibration(true);
        alarm.setVibrationPattern(new long[]{0, 400, 200, 400, 200, 400});
        nm.createNotificationChannel(alarm);
        Intent oeffnen = new Intent(Intent.ACTION_VIEW, Uri.parse(url), ctx, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(ctx, id, oeffnen,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(ctx, dringend ? KANAL_DRINGEND : KANAL)
                .setCategory(dringend ? Notification.CATEGORY_ALARM : Notification.CATEGORY_RECOMMENDATION)
                .setSmallIcon(ctx.getResources().getIdentifier("ic_launcher_vordergrund", "mipmap", ctx.getPackageName()))
                .setColor(dringend ? 0xFFC0392B : 0xFF2F5D50)
                .setContentTitle(titel)
                .setContentText(text.split("\n")[0])
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .build();
        nm.notify(id, n);
    }
}
