package com.scalping.terminal;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;

/** Notification channels and alert posting. {@link #show} is called from Python through Chaquopy. */
public final class Notifier {
    static final String CH_ENGINE = "engine";
    static final String CH_ALERTS = "alerts";
    private static Context app;
    private static int nextId = 100;

    private Notifier() {}

    static void init(Context ctx) {
        app = ctx.getApplicationContext();
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = app.getSystemService(NotificationManager.class);
            NotificationChannel engine = new NotificationChannel(CH_ENGINE, "Engine running", NotificationManager.IMPORTANCE_LOW);
            engine.setDescription("Shown while the market engine runs in the background");
            NotificationChannel alerts = new NotificationChannel(CH_ALERTS, "Trading alerts", NotificationManager.IMPORTANCE_HIGH);
            alerts.setDescription("Validated signals, pump/dump stages, confirmed trends and major news");
            nm.createNotificationChannel(engine);
            nm.createNotificationChannel(alerts);
        }
    }

    static PendingIntent openApp(Context ctx) {
        Intent i = new Intent(ctx, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(ctx, 0, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    static Notification.Builder builder(Context ctx, String channel) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(ctx, channel) : new Notification.Builder(ctx);
        return b.setSmallIcon(R.drawable.ic_stat).setContentIntent(openApp(ctx)).setColor(0xFF22C55E);
    }

    /** Post an alert notification (no-op if notifications are not permitted). */
    public static synchronized void show(String title, String body) {
        if (app == null) return;
        if (Build.VERSION.SDK_INT >= 33
                && app.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            return;
        }
        Notification.Builder b = builder(app, CH_ALERTS)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setAutoCancel(true);
        if (Build.VERSION.SDK_INT < 26) b.setPriority(Notification.PRIORITY_HIGH);
        app.getSystemService(NotificationManager.class).notify(nextId++, b.build());
        if (nextId > 1000) nextId = 100;
    }
}
