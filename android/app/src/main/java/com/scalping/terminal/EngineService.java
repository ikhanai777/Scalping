package com.scalping.terminal;

import android.app.Notification;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import com.chaquo.python.PyException;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Foreground service that runs the Python engine (Binance streams, indicators, Trend Catcher, pump radar,
 * signals, paper broker and the local web server on 127.0.0.1) so it keeps working with the screen off.
 */
public class EngineService extends Service {
    static final int PORT = 8765;
    private static final String TAG = "ScalpingEngine";
    private static Thread engineThread;
    static volatile String lastError;

    @Override
    public void onCreate() {
        super.onCreate();
        Notifier.init(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = Notifier.builder(this, Notifier.CH_ENGINE)
                .setContentTitle("Scalping engine running")
                .setContentText("Live market data · paper trading · tap to open")
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(1, n);
        }
        startEngine();
        return START_STICKY;
    }

    private synchronized void startEngine() {
        if (engineThread != null && engineThread.isAlive()) return;
        if (!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
        final String filesDir = getFilesDir().getAbsolutePath();
        engineThread = new Thread(() -> {
            try {
                Python.getInstance().getModule("scalper.mobile").callAttr("android_main", filesDir, PORT);
            } catch (PyException e) {
                lastError = e.getMessage();
                Log.e(TAG, "engine stopped with an error", e);
            }
        }, "python-engine");
        engineThread.setDaemon(true);
        engineThread.start();
    }

    @Override
    public void onDestroy() {
        try {
            if (Python.isStarted()) Python.getInstance().getModule("scalper.mobile").callAttr("stop");
        } catch (PyException e) {
            Log.w(TAG, "stop failed", e);
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
