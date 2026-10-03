package com.scalping.terminal;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import java.net.HttpURLConnection;
import java.net.URL;

/** Hosts the terminal UI (served by the on-device engine) in a full-screen WebView. */
public class MainActivity extends Activity {
    private static final String BASE = "http://127.0.0.1:" + EngineService.PORT;
    private WebView web;
    private View loading;
    private TextView loadingText;
    private final Handler main = new Handler(Looper.getMainLooper());
    private volatile boolean polling;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Notifier.init(this);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0B0F14"));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0B0F14"));
        web.setVisibility(View.INVISIBLE);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setSupportMultipleWindows(false);
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if ("127.0.0.1".equals(u.getHost())) return false;
                openExternal(u.toString());           // news links etc. open in the browser
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                web.setVisibility(View.VISIBLE);
                loading.setVisibility(View.GONE);
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        ProgressBar pb = new ProgressBar(this);
        loadingText = new TextView(this);
        loadingText.setTextColor(Color.parseColor("#7D8B9A"));
        loadingText.setGravity(Gravity.CENTER);
        loadingText.setPadding(48, 32, 48, 0);
        loadingText.setText("Starting the market engine…\nFirst start loads history and can take 1–2 minutes.");
        box.addView(pb);
        box.addView(loadingText);
        loading = box;
        root.addView(box, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // Keep content clear of the status/navigation bars (Android 15 draws edge-to-edge).
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets i = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                v.setPadding(i.left, i.top, i.right, i.bottom);
            } else {
                v.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                        insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        setContentView(root);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        Intent svc = new Intent(this, EngineService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc); else startService(svc);
        waitForEngine();
    }

    /** Poll the local engine until its API answers, then load the UI. */
    private void waitForEngine() {
        if (polling) return;
        polling = true;
        new Thread(() -> {
            long t0 = System.currentTimeMillis();
            while (polling) {
                if (ping()) {
                    main.post(() -> web.loadUrl(BASE + "/"));
                    polling = false;
                    return;
                }
                String err = EngineService.lastError;
                long secs = (System.currentTimeMillis() - t0) / 1000;
                main.post(() -> loadingText.setText(err != null
                        ? "The engine stopped with an error:\n" + err
                        : "Starting the market engine… " + secs + "s\nFirst start loads history and can take 1–2 minutes."));
                try { Thread.sleep(700); } catch (InterruptedException e) { return; }
            }
        }, "engine-wait").start();
    }

    private static boolean ping() {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(BASE + "/api/status").openConnection();
            c.setConnectTimeout(500);
            c.setReadTimeout(1500);
            int code = c.getResponseCode();
            c.disconnect();
            return code == 200;
        } catch (Exception e) {
            return false;
        }
    }

    private void openExternal(String url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (Exception ignored) {
            // no browser installed
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();                 // the UI pushes history entries for sheets and sub-screens
        } else {
            moveTaskToBack(true);         // keep the engine running in the background
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }

    @Override
    protected void onPause() {
        if (web != null) web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        polling = false;
        if (web != null) web.destroy();
        super.onDestroy();
    }

    /** Methods callable from the web UI as window.AndroidBridge.*. */
    final class Bridge {
        @JavascriptInterface
        public String appVersion() {
            try {
                return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
            } catch (PackageManager.NameNotFoundException e) {
                return "?";
            }
        }

        @JavascriptInterface
        public void openExternal(String url) {
            main.post(() -> MainActivity.this.openExternal(url));
        }

        /** Restart the whole app process so the engine reloads its settings. */
        @JavascriptInterface
        public void restartEngine() {
            main.post(() -> {
                stopService(new Intent(MainActivity.this, EngineService.class));
                Intent launch = getPackageManager().getLaunchIntentForPackage(getPackageName());
                if (launch != null) startActivity(Intent.makeRestartActivityTask(launch.getComponent()));
                Runtime.getRuntime().exit(0);
            });
        }

        /** Stop the background engine and close the app. */
        @JavascriptInterface
        public void exitApp() {
            main.post(() -> {
                stopService(new Intent(MainActivity.this, EngineService.class));
                finishAndRemoveTask();
                Runtime.getRuntime().exit(0);
            });
        }
    }
}
