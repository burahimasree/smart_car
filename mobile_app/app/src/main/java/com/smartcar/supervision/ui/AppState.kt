package com.smartcar.supervision.ui

import com.smartcar.supervision.data.HealthStatus
import com.smartcar.supervision.data.TelemetrySnapshot

sealed class ConnectionStatus {
    data object Online : ConnectionStatus()
    data object Offline : ConnectionStatus()
    data class Error(val message: String) : ConnectionStatus()
}

data class AppState(
    val connection: ConnectionStatus = ConnectionStatus.Offline,
    val appStatus: AppStatus = AppStatus.OFFLINE,
    val blockingReason: String? = null,
    val status: TelemetrySnapshot? = null,
    val telemetry: TelemetrySnapshot? = null,
    val health: HealthStatus? = null,
    val lastStatusAt: Long? = null,
    val lastTelemetryAt: Long? = null,
    val lastConnectAttemptAt: Long? = null,
    val intentInFlight: Boolean = false,
    val task: TaskState = TaskState(),
    val lastIntentResult: String? = null,
    val lastIntentSent: String? = null,
    val lastIntentAt: Long? = null,
    val lastRemoteEvent: String? = null,
    val logs: List<String> = emptyList(),
    val logExportResult: String? = null,
    val debugPanelVisible: Boolean = false,
    val settings: com.smartcar.supervision.data.AppSettings? = null,
)

enum class AppStatus {
    OFFLINE,
    CONNECTING,
    ONLINE_IDLE,
    ONLINE_BUSY,
    ONLINE_EXECUTING_TASK,
    ERROR,
}

enum class TaskType {
    NONE,
    SCAN_OBSERVE_STOP,
}

enum class TaskPhase {
    IDLE,
    EXECUTING,
    OBSERVE,
    STOPPED,
}

data class TaskState(
    val type: TaskType = TaskType.NONE,
    val phase: TaskPhase = TaskPhase.IDLE,
    val label: String = "",
)
