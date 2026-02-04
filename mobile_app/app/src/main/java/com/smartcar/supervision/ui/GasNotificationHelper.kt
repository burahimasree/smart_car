package com.smartcar.supervision.ui

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.smartcar.supervision.R

class GasNotificationHelper(private val context: Context) {
    companion object {
        const val CHANNEL_ID = "gas_alerts"
        private const val CHANNEL_NAME = "Gas Alerts"
        private const val CHANNEL_DESC = "MQ2 gas sensor warnings"
        private const val NOTIFICATION_ID = 2001
    }

    fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(CHANNEL_ID, CHANNEL_NAME, NotificationManager.IMPORTANCE_HIGH)
        channel.description = CHANNEL_DESC
        channel.enableVibration(true)
        channel.enableLights(true)
        channel.lockscreenVisibility = NotificationCompat.VISIBILITY_PUBLIC
        manager.createNotificationChannel(channel)
    }

    fun showAlert(severity: String, level: Int?) {
        val title = if (severity == "danger") "Gas danger detected" else "Gas warning detected"
        val levelText = level?.let { "MQ2: $it" } ?: "MQ2 level unavailable"
        val builder = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.app_icon_smart_car)
            .setContentTitle(title)
            .setContentText(levelText)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setAutoCancel(false)
            .setOngoing(true)

        NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, builder.build())
    }

    fun clear() {
        NotificationManagerCompat.from(context).cancel(NOTIFICATION_ID)
    }
}
