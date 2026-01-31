package com.smartcar.supervision.ui

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.smartcar.supervision.BuildConfig
import com.smartcar.supervision.data.AppSettings
import com.smartcar.supervision.data.IntentResult
import com.smartcar.supervision.data.RobotRepository
import com.smartcar.supervision.data.SettingsStore
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File
import java.net.URI
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

class AppViewModel(
    private val repo: RobotRepository = RobotRepository(),
) : ViewModel() {
    private val _state = MutableStateFlow(AppState())
    val state: StateFlow<AppState> = _state.asStateFlow()
    private var lastTelemetryLogAt: Long = 0L
    private var pollJob: Job? = null
    private var settingsStore: SettingsStore? = null
    private var contextBound = false

    init {
        appendLog("app_start")
        _state.value = _state.value.copy(appStatus = AppStatus.CONNECTING)
    }

    fun bindContext(context: Context) {
        if (contextBound) return
        contextBound = true
        val store = SettingsStore(context.applicationContext)
        settingsStore = store
        val (defaultIp, defaultPort) = parseDefaultIpPort()
        val settings = store.loadDefaults(defaultIp, defaultPort)
        _state.value = _state.value.copy(
            settings = settings,
            debugPanelVisible = settings.debugEnabled,
        )
        repo.updateBaseUrl(settings.baseUrl())
        startPolling(settings.pollIntervalMs)
        refreshNow()
        appendLog("settings_loaded:${settings.baseUrl()}")
    }

    fun updateSettings(settings: AppSettings) {
        settingsStore?.save(settings)
        _state.value = _state.value.copy(
            settings = settings,
            debugPanelVisible = settings.debugEnabled,
        )
        repo.updateBaseUrl(settings.baseUrl())
        startPolling(settings.pollIntervalMs)
        refreshNow()
        appendLog("settings_updated:${settings.baseUrl()}")
    }

    fun refreshNow() {
        viewModelScope.launch {
            val now = System.currentTimeMillis()
            _state.value = _state.value.copy(lastConnectAttemptAt = now)

            val snapshotResult = repo.fetchSnapshotOnce()
            snapshotResult.onSuccess { snapshot ->
                val current = _state.value
                val remoteEvent = snapshot.telemetry?.remote_event?.let { it.toString() }
                _state.value = current.copy(
                    connection = ConnectionStatus.Online,
                    status = snapshot.status ?: current.status,
                    telemetry = snapshot.telemetry ?: current.telemetry,
                    lastStatusAt = if (snapshot.status != null) now else current.lastStatusAt,
                    lastTelemetryAt = if (snapshot.telemetry != null) now else current.lastTelemetryAt,
                    lastRemoteEvent = remoteEvent ?: current.lastRemoteEvent,
                )
                appendLog("refresh_ok")
            }.onFailure { err ->
                _state.value = _state.value.copy(
                    connection = ConnectionStatus.Error(err.message ?: "refresh_error")
                )
                appendLog("refresh_error:${err.message ?: "unknown"}")
            }

            val health = repo.checkHealth()
            health.onSuccess {
                _state.value = _state.value.copy(health = it)
            }.onFailure { err ->
                appendLog("health_error:${err.message ?: "unknown"}")
            }

            refreshAppStatus()
        }
    }

    private fun startPolling(pollMs: Long) {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            repo.snapshotStream(pollMs).collect { result ->
                val now = System.currentTimeMillis()
                _state.value = _state.value.copy(lastConnectAttemptAt = now)
                result.onSuccess { snapshot ->
                    val current = _state.value
                    val remoteEvent = snapshot.telemetry?.remote_event?.let { it.toString() }
                    _state.value = current.copy(
                        connection = ConnectionStatus.Online,
                        status = snapshot.status ?: current.status,
                        telemetry = snapshot.telemetry ?: current.telemetry,
                        lastStatusAt = if (snapshot.status != null) now else current.lastStatusAt,
                        lastTelemetryAt = if (snapshot.telemetry != null) now else current.lastTelemetryAt,
                        lastRemoteEvent = remoteEvent ?: current.lastRemoteEvent,
                    )
                    if (now - lastTelemetryLogAt > 30_000) {
                        appendLog("telemetry_ok")
                        lastTelemetryLogAt = now
                    }
                    refreshAppStatus()
                }.onFailure { err ->
                    _state.value = _state.value.copy(
                        connection = ConnectionStatus.Error(err.message ?: "telemetry_error")
                    )
                    appendLog("telemetry_error:${err.message ?: "unknown"}")
                    refreshAppStatus()
                }
            }
        }
    }

    fun sendIntent(
        intent: String,
        extras: Map<String, Any> = emptyMap(),
        onComplete: ((IntentResult) -> Unit)? = null,
    ) {
        viewModelScope.launch {
            _state.value = _state.value.copy(intentInFlight = true)
            _state.value = _state.value.copy(lastIntentSent = intent, lastIntentAt = System.currentTimeMillis())
            appendLog("intent_send:$intent")
            refreshAppStatus()
            val result = repo.sendIntent(intent, extras)
            val message = when (result) {
                is IntentResult.Accepted -> "accepted"
                is IntentResult.Rejected -> "rejected: ${result.reason}"
                is IntentResult.TimedOut -> "timed_out: ${result.reason}"
                is IntentResult.Failed -> "failed: ${result.reason}"
            }
            _state.value = _state.value.copy(lastIntentResult = message)
            _state.value = _state.value.copy(intentInFlight = false)
            appendLog("intent_result:$intent:$message")
            refreshAppStatus()
            onComplete?.invoke(result)
        }
    }

    fun startScanObserveTask() {
        _state.value = _state.value.copy(
            task = TaskState(
                type = TaskType.SCAN_OBSERVE_STOP,
                phase = TaskPhase.EXECUTING,
                label = "Scanning",
            )
        )
        appendLog("task_start:scan_observe_stop")
        sendIntent("scan") { result ->
            val label = when (result) {
                is IntentResult.Accepted -> "Observe"
                is IntentResult.Rejected -> "Scan rejected"
                is IntentResult.TimedOut -> "Scan timed out"
                is IntentResult.Failed -> "Scan failed"
            }
            val phase = when (result) {
                is IntentResult.Accepted -> TaskPhase.OBSERVE
                else -> TaskPhase.STOPPED
            }
            _state.value = _state.value.copy(
                task = TaskState(
                    type = TaskType.SCAN_OBSERVE_STOP,
                    phase = phase,
                    label = label,
                )
            )
            appendLog("task_update:scan_observe_stop:${phase.name}")
            refreshAppStatus()
        }
    }

    fun markObservation() {
        val current = _state.value.task
        if (current.type != TaskType.SCAN_OBSERVE_STOP) return
        _state.value = _state.value.copy(
            task = current.copy(
                phase = TaskPhase.OBSERVE,
                label = "Observe",
            )
        )
        appendLog("task_mark_observe")
        refreshAppStatus()
    }

    fun stopTask() {
        val current = _state.value.task
        if (current.type == TaskType.NONE) {
            sendIntent("stop")
            return
        }
        _state.value = _state.value.copy(
            task = current.copy(
                phase = TaskPhase.EXECUTING,
                label = "Stopping",
            )
        )
        sendIntent("stop") {
            _state.value = _state.value.copy(
                task = current.copy(
                    phase = TaskPhase.STOPPED,
                    label = "Stopped",
                )
            )
            appendLog("task_stop")
            refreshAppStatus()
        }
    }

    fun clearTask() {
        _state.value = _state.value.copy(task = TaskState())
        appendLog("task_clear")
        refreshAppStatus()
    }

    fun toggleDebugPanel() {
        val current = _state.value
        val newValue = !current.debugPanelVisible
        _state.value = current.copy(debugPanelVisible = newValue)
        val settings = current.settings
        if (settings != null) {
            val updated = settings.copy(debugEnabled = newValue)
            settingsStore?.save(updated)
            _state.value = _state.value.copy(settings = updated)
        }
    }

    fun appendLog(message: String) {
        val timestamp = LocalDateTime.now().format(DateTimeFormatter.ISO_LOCAL_TIME)
        val entry = "$timestamp $message"
        val updated = (_state.value.logs + entry).takeLast(200)
        _state.value = _state.value.copy(logs = updated)
    }

    fun exportLogs(context: Context) {
        val now = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss"))
        val file = File(context.filesDir, "smartcar_log_$now.txt")
        val content = _state.value.logs.joinToString(separator = "\n")
        try {
            file.writeText(content)
            _state.value = _state.value.copy(logExportResult = "saved: ${file.absolutePath}")
            appendLog("log_export:ok")
        } catch (err: Exception) {
            _state.value = _state.value.copy(logExportResult = "error: ${err.message ?: "unknown"}")
            appendLog("log_export:error")
        }
    }

    private fun refreshAppStatus() {
        val current = _state.value
        val sessionActive = (current.telemetry?.remote_session_active == true) ||
            (current.status?.remote_session_active == true)
        val busy = current.intentInFlight
        val executingTask = current.task.phase == TaskPhase.EXECUTING

        val status = when (current.connection) {
            ConnectionStatus.Offline -> AppStatus.OFFLINE
            is ConnectionStatus.Error -> AppStatus.ERROR
            ConnectionStatus.Online -> {
                if (!sessionActive) {
                    AppStatus.ERROR
                } else if (executingTask) {
                    AppStatus.ONLINE_EXECUTING_TASK
                } else if (busy) {
                    AppStatus.ONLINE_BUSY
                } else {
                    AppStatus.ONLINE_IDLE
                }
            }
        }

        val blockingReason = when {
            status == AppStatus.OFFLINE -> "offline"
            status == AppStatus.ERROR && !sessionActive -> "no_remote_session"
            status == AppStatus.ERROR -> "error"
            executingTask -> "task_executing"
            busy -> "intent_in_flight"
            else -> null
        }

        _state.value = current.copy(appStatus = status, blockingReason = blockingReason)
    }

    private fun parseDefaultIpPort(): Pair<String, Int> {
        return try {
            val uri = URI(BuildConfig.ROBOT_BASE_URL)
            val host = uri.host ?: BuildConfig.ROBOT_BASE_URL
                .removePrefix("http://")
                .removePrefix("https://")
                .substringBefore("/")
                .substringBefore(":")
            val port = if (uri.port != -1) uri.port else if (uri.scheme == "https") 443 else 80
            host to port
        } catch (err: Exception) {
            val cleaned = BuildConfig.ROBOT_BASE_URL
                .removePrefix("http://")
                .removePrefix("https://")
            val host = cleaned.substringBefore("/").substringBefore(":")
            val port = cleaned.substringAfter(":", "80").substringBefore("/").toIntOrNull() ?: 80
            host to port
        }
    }
}
