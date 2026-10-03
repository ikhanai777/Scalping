# Called from Python via Chaquopy (reflection) and from JavaScript (@JavascriptInterface).
-keep class com.scalping.terminal.** { *; }
-keepclassmembers class * { @android.webkit.JavascriptInterface <methods>; }
