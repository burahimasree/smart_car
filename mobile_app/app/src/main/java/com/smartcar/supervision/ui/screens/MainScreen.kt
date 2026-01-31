package com.smartcar.supervision.ui.screens

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.view.ViewGroup
import android.widget.ImageView
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Tune
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import com.smartcar.supervision.BuildConfig
import com.smartcar.supervision.data.AppSettings
import com.smartcar.supervision.ui.AppStatus
import com.smartcar.supervision.ui.AppViewModel
import com.smartcar.supervision.ui.ConnectionStatus
import com.smartcar.supervision.ui.TaskPhase
import com.smartcar.supervision.ui.TaskType
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

private enum class MainTab(val label: String) {
    STATUS("Status"),
    CONTROL("Control"),
    VISION("Vision"),
    SETTINGS("Settings"),
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
fun MainScreen(viewModel: AppViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val context = LocalContext.current
    var selectedTab by rememberSaveable { mutableStateOf(MainTab.STATUS) }

    LaunchedEffect(Unit) {
        viewModel.bindContext(context)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Text("Smart Car Console")
                        Text(
                            "${state.appStatus} • ${state.blockingReason ?: "ready"}",
                            style = MaterialTheme.typography.labelSmall
                        )
                    }
                },
                actions = {
                    IconButton(onClick = { viewModel.refreshNow() }) {
                        Icon(imageVector = Icons.Filled.Refresh, contentDescription = "Refresh")
                    }
                }
            )
        },
        bottomBar = {
            NavigationBar {
                NavigationBarItem(
                    selected = selectedTab == MainTab.STATUS,
                    onClick = { selectedTab = MainTab.STATUS },
                    icon = { Icon(Icons.Filled.Info, contentDescription = null) },
                    label = { Text(MainTab.STATUS.label) },
                )
                NavigationBarItem(
                    selected = selectedTab == MainTab.CONTROL,
                    onClick = { selectedTab = MainTab.CONTROL },
                    icon = { Icon(Icons.Filled.Tune, contentDescription = null) },
                    label = { Text(MainTab.CONTROL.label) },
                )
                NavigationBarItem(
                    selected = selectedTab == MainTab.VISION,
                    onClick = { selectedTab = MainTab.VISION },
                    icon = { Icon(Icons.Filled.Visibility, contentDescription = null) },
                    label = { Text(MainTab.VISION.label) },
                )
                NavigationBarItem(
                    selected = selectedTab == MainTab.SETTINGS,
                    onClick = { selectedTab = MainTab.SETTINGS },
                    icon = { Icon(Icons.Filled.Settings, contentDescription = null) },
                    label = { Text(MainTab.SETTINGS.label) },
                )
            }
        }
    ) { innerPadding ->
        val contentModifier = Modifier
            .fillMaxSize()
            .padding(innerPadding)

        when (selectedTab) {
            MainTab.STATUS -> StatusScreen(
                modifier = contentModifier,
                viewModel = viewModel,
                onOpenSettings = { selectedTab = MainTab.SETTINGS },
            )
            MainTab.CONTROL -> ControlScreen(
                modifier = contentModifier,
                viewModel = viewModel,
                onOpenStatus = { selectedTab = MainTab.STATUS },
                onOpenSettings = { selectedTab = MainTab.SETTINGS },
            )
            MainTab.VISION -> VisionScreen(
                modifier = contentModifier,
                viewModel = viewModel,
                onOpenStatus = { selectedTab = MainTab.STATUS },
                onOpenSettings = { selectedTab = MainTab.SETTINGS },
            )
            MainTab.SETTINGS -> SettingsScreen(
                modifier = contentModifier,
                viewModel = viewModel,
            )
        }
    }
}

@Composable
private fun StatusScreen(
    modifier: Modifier,
    viewModel: AppViewModel,
    onOpenSettings: () -> Unit,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val now = System.currentTimeMillis()
    val sessionActive = (state.telemetry?.remote_session_active == true) || (state.status?.remote_session_active == true)
    val busy = state.intentInFlight || state.task.phase == TaskPhase.EXECUTING
    val telemetryAgeMs = state.lastTelemetryAt?.let { now - it }
    val telemetryStale = telemetryAgeMs != null && telemetryAgeMs > 5_000
    val telemetryFreshness = when {
        state.lastTelemetryAt == null -> "unknown"
        telemetryStale -> "stale"
        else -> "ok"
    }
    val streamUrl = (state.telemetry?.stream_url ?: state.status?.stream_url).orEmpty().trim()

    Column(
        modifier = modifier
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Card {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Connection", style = MaterialTheme.typography.titleMedium)
                Text(
                    when (val c = state.connection) {
                        ConnectionStatus.Online -> "online"
                        ConnectionStatus.Offline -> "offline"
                        is ConnectionStatus.Error -> "error: ${c.message}"
                    }
                )
                Text("App state: ${state.appStatus}")
                Text("Blocking: ${state.blockingReason ?: "none"}")
                Text("Health: ${confidenceLabel(state.health?.ok, state.lastStatusAt)}")
                Text("Remote session: ${confidenceLabel(state.telemetry?.remote_session_active ?: state.status?.remote_session_active, state.lastTelemetryAt)}")
                Text("Telemetry freshness: $telemetryFreshness")
                Text("Status mode: ${confidenceLabel(state.status?.mode, state.lastStatusAt)}")
                Text("Status display: ${confidenceLabel(state.status?.display_text, state.lastStatusAt)}")
                Text("Telemetry mode: ${confidenceLabel(state.telemetry?.mode, state.lastTelemetryAt)}")
                Text("Telemetry display: ${confidenceLabel(state.telemetry?.display_text, state.lastTelemetryAt)}")
                Text("Vision mode: ${confidenceLabel(state.telemetry?.vision_mode, state.lastTelemetryAt)}")
                Text("Safety stop: ${confidenceLabel(state.telemetry?.safety_stop, state.lastTelemetryAt)}")
                Text("Intent busy: ${triState(if (busy) true else false)}")
                Text("Last connect attempt: ${formatTimestamp(state.lastConnectAttemptAt)}")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { viewModel.refreshNow() }) {
                        Text("Retry now")
                    }
                    TextButton(onClick = onOpenSettings) {
                        Text("Open settings")
                    }
                }
            }
        }

        Card {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Failure surface", style = MaterialTheme.typography.titleMedium)
                if (state.connection is ConnectionStatus.Error) {
                    Text("Network failure: ${(state.connection as ConnectionStatus.Error).message}")
                    Text("Operator action: check Tailscale connectivity")
                }
                if (!sessionActive) {
                    Text("Session loss: remote session inactive")
                    Text("Operator action: open telemetry or send intent when session re-established")
                }
                if (state.telemetry == null) {
                    Text("Partial telemetry: /telemetry missing")
                    Text("Operator action: wait for telemetry refresh")
                }
                if (state.status == null) {
                    Text("Partial telemetry: /status missing")
                    Text("Operator action: wait for status refresh")
                }
                if (state.telemetry?.vision_mode == "on_with_stream" && streamUrl.isBlank()) {
                    Text("Vision unavailable: stream URL not provided")
                    Text("Operator action: confirm backend stream URL")
                }
                if (state.lastIntentResult?.startsWith("rejected") == true) {
                    Text("Intent rejected: ${state.lastIntentResult}")
                    Text("Operator action: check session and intent validity")
                }
                if (state.lastIntentResult?.startsWith("timed_out") == true) {
                    Text("Intent timeout: ${state.lastIntentResult}")
                    Text("Operator action: retry manually when online")
                }
                if (state.lastIntentResult?.startsWith("failed") == true) {
                    Text("Intent failure: ${state.lastIntentResult}")
                    Text("Operator action: check network and retry")
                }
                if (state.connection is ConnectionStatus.Online && sessionActive &&
                    state.status != null && state.telemetry != null &&
                    state.lastIntentResult == null && !busy
                ) {
                    Text("No active failures")
                }
            }
        }

        Card {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Diagnostics", style = MaterialTheme.typography.titleMedium)
                Text("App version: ${BuildConfig.VERSION_NAME}")
                Text("Robot base URL: ${state.settings?.baseUrl() ?: BuildConfig.ROBOT_BASE_URL}")
                Text("Last status: ${formatTimestamp(state.lastStatusAt)}")
                Text("Last telemetry: ${formatTimestamp(state.lastTelemetryAt)}")
                Text("Log export: ${state.logExportResult ?: "not exported"}")

                Button(onClick = { viewModel.exportLogs(context) }) {
                    Text("Export logs")
                }

                Text("Recent logs")
                val recent = state.logs.takeLast(8)
                if (recent.isEmpty()) {
                    Text("No logs yet")
                } else {
                    recent.forEach { line ->
                        Text(line, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    }
                }

                Button(onClick = { viewModel.toggleDebugPanel() }) {
                    Text(if (state.debugPanelVisible) "Hide debug panel" else "Show debug panel")
                }
                if (state.debugPanelVisible) {
                    Text("Debug panel")
                    Text("Last intent: ${state.lastIntentSent ?: "unknown"}")
                    Text("Last intent result: ${state.lastIntentResult ?: "unknown"}")
                    Text("Last remote_event: ${state.lastRemoteEvent ?: "unknown"}")
                    Text("Last status ts: ${formatTimestamp(state.lastStatusAt)}")
                    Text("Last telemetry ts: ${formatTimestamp(state.lastTelemetryAt)}")
                }
            }
        }

        Card {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Known limitations", style = MaterialTheme.typography.titleMedium)
                Text("- No auto-retry for intents")
                Text("- Telemetry may be stale or missing fields")
                Text("- Streaming depends on backend-provided URL and format")
            }
        }
        Spacer(modifier = Modifier.height(12.dp))
    }
}

@Composable
private fun ControlScreen(
    modifier: Modifier,
    viewModel: AppViewModel,
    onOpenStatus: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val intentsEnabled = state.appStatus == AppStatus.ONLINE_IDLE
    val stopEnabled = state.appStatus == AppStatus.ONLINE_EXECUTING_TASK || state.appStatus == AppStatus.ONLINE_BUSY
    val sessionActive = (state.telemetry?.remote_session_active == true) || (state.status?.remote_session_active == true)
    var confirmScan by rememberSaveable { mutableStateOf(false) }
    var confirmStop by rememberSaveable { mutableStateOf(false) }
    var confirmStartForward by rememberSaveable { mutableStateOf(false) }
    var actionBlockedReason by rememberSaveable { mutableStateOf<String?>(null) }

    Column(
        modifier = modifier
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        if (!intentsEnabled || !sessionActive) {
            Card {
                Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("Control offline", style = MaterialTheme.typography.titleMedium)
                    Text("Reason: ${state.blockingReason ?: "session inactive"}")
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onClick = onOpenStatus) { Text("View status") }
                        TextButton(onClick = onOpenSettings) { Text("Open settings") }
                    }
                }
            }
        }

        Card {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Task Flow", style = MaterialTheme.typography.titleMedium)
                Text("Task: ${state.task.type}")
                Text("Phase: ${state.task.phase}")
                Text("Status: ${state.task.label}")
                Text("Blocking: ${state.blockingReason ?: "none"}")
                Button(
                    onClick = {
                        if (intentsEnabled) {
                            confirmScan = true
                        } else {
                            actionBlockedReason = state.blockingReason ?: "invalid_state"
                        }
                    },
                    enabled = intentsEnabled
                ) {
                    Text("Start scan → observe → stop")
                }
                Button(
                    onClick = {
                        if (intentsEnabled && state.task.phase == TaskPhase.OBSERVE) {
                            viewModel.markObservation()
                        } else {
                            actionBlockedReason = state.blockingReason ?: "invalid_state"
                        }
                    },
                    enabled = intentsEnabled && state.task.phase == TaskPhase.OBSERVE
                ) {
                    Text("Mark observe")
                }
                Button(
                    onClick = {
                        if (stopEnabled && state.task.type != TaskType.NONE) {
                            confirmStop = true
                        } else {
                            actionBlockedReason = state.blockingReason ?: "invalid_state"
                        }
                    },
                    enabled = stopEnabled && state.task.type != TaskType.NONE
                ) {
                    Text("Stop task")
                }
                Button(
                    onClick = {
                        if (!state.intentInFlight && state.task.type != TaskType.NONE) {
                            viewModel.clearTask()
                        } else {
                            actionBlockedReason = state.blockingReason ?: "invalid_state"
                        }
                    },
                    enabled = !state.intentInFlight && state.task.type != TaskType.NONE
                ) {
                    Text("Clear task")
                }
            }
        }

        Card {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Intent", style = MaterialTheme.typography.titleMedium)
                if (!intentsEnabled) {
                    Text("Intents disabled: offline, no remote session, or busy")
                }
                Button(onClick = { viewModel.sendIntent("enable_vision") }, enabled = intentsEnabled) { Text("Enable vision") }
                Button(onClick = { viewModel.sendIntent("disable_vision") }, enabled = intentsEnabled) { Text("Disable vision") }
                Button(onClick = { viewModel.sendIntent("enable_stream") }, enabled = intentsEnabled) { Text("Enable stream") }
                Button(onClick = { viewModel.sendIntent("disable_stream") }, enabled = intentsEnabled) { Text("Disable stream") }
                HorizontalDivider()
                Button(onClick = { confirmScan = true }, enabled = intentsEnabled) { Text("Scan") }
                Button(onClick = { confirmStop = true }, enabled = intentsEnabled) { Text("Stop") }
                Button(onClick = { viewModel.sendIntent("rotate_left") }, enabled = intentsEnabled) { Text("Rotate left") }
                Button(onClick = { viewModel.sendIntent("rotate_right") }, enabled = intentsEnabled) { Text("Rotate right") }
                Button(onClick = { confirmStartForward = true }, enabled = intentsEnabled) { Text("Start forward") }
                Text("Last intent result: ${state.lastIntentResult ?: ""}")
            }
        }
        Spacer(modifier = Modifier.height(12.dp))
    }

    if (actionBlockedReason != null) {
        AlertDialog(
            onDismissRequest = { actionBlockedReason = null },
            title = { Text("Action blocked") },
            text = { Text("Reason: ${actionBlockedReason}") },
            confirmButton = {
                TextButton(onClick = { actionBlockedReason = null }) { Text("OK") }
            }
        )
    }

    if (confirmScan) {
        AlertDialog(
            onDismissRequest = { confirmScan = false },
            title = { Text("Confirm scan") },
            text = { Text("Send scan intent to the robot?") },
            confirmButton = {
                Button(onClick = {
                    confirmScan = false
                    viewModel.startScanObserveTask()
                }) { Text("Confirm") }
            },
            dismissButton = {
                TextButton(onClick = { confirmScan = false }) { Text("Cancel") }
            }
        )
    }

    if (confirmStop) {
        AlertDialog(
            onDismissRequest = { confirmStop = false },
            title = { Text("Confirm stop") },
            text = { Text("Send stop intent to the robot?") },
            confirmButton = {
                Button(onClick = {
                    confirmStop = false
                    viewModel.stopTask()
                }) { Text("Confirm") }
            },
            dismissButton = {
                TextButton(onClick = { confirmStop = false }) { Text("Cancel") }
            }
        )
    }

    if (confirmStartForward) {
        AlertDialog(
            onDismissRequest = { confirmStartForward = false },
            title = { Text("Confirm forward") },
            text = { Text("Send start forward intent to the robot?") },
            confirmButton = {
                Button(onClick = {
                    confirmStartForward = false
                    viewModel.sendIntent("start")
                }) { Text("Confirm") }
            },
            dismissButton = {
                TextButton(onClick = { confirmStartForward = false }) { Text("Cancel") }
            }
        )
    }
}

@Composable
private fun VisionScreen(
    modifier: Modifier,
    viewModel: AppViewModel,
    onOpenStatus: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val sessionActive = (state.telemetry?.remote_session_active == true) || (state.status?.remote_session_active == true)
    val intentsEnabled = state.appStatus == AppStatus.ONLINE_IDLE && sessionActive
    val streamUrl = resolveStreamUrl(state.telemetry?.stream_url ?: state.status?.stream_url, state.settings)
    val visionMode = state.telemetry?.vision_mode ?: state.status?.vision_mode
    val canStream = intentsEnabled && visionMode == "on_with_stream" && streamUrl.isNotBlank()
    var isStreaming by rememberSaveable { mutableStateOf(false) }
    var streamError by rememberSaveable { mutableStateOf<String?>(null) }
    var overlayEnabled by rememberSaveable { mutableStateOf(false) }

    LaunchedEffect(canStream) {
        if (!canStream) {
            isStreaming = false
        }
    }

    Column(
        modifier = modifier
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        if (!intentsEnabled) {
            Card {
                Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("Vision offline", style = MaterialTheme.typography.titleMedium)
                    Text("Reason: ${state.blockingReason ?: "session inactive"}")
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onClick = onOpenStatus) { Text("View status") }
                        TextButton(onClick = onOpenSettings) { Text("Open settings") }
                    }
                }
            }
        }

        Card {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Streaming", style = MaterialTheme.typography.titleMedium)
                Text("Vision mode: ${visionMode ?: "unknown"}")
                if (streamUrl.isBlank()) {
                    Text("No stream URL provided by backend")
                    Text("Streaming is disabled until a valid URL is present")
                    Text("No auto-connect is performed")
                } else if (!intentsEnabled) {
                    Text("Streaming disabled: ${state.blockingReason ?: "invalid_state"}")
                    Text("No auto-connect is performed")
                } else if (visionMode != "on_with_stream") {
                    Text("Streaming disabled: vision mode is not on_with_stream")
                    Text("Enable streaming via intent first")
                } else {
                    if (streamError != null) {
                        Text("Stream error: ${streamError}")
                    }
                    if (!isStreaming) {
                        Button(onClick = {
                            streamError = null
                            isStreaming = true
                        }, enabled = canStream) {
                            Text("Start stream")
                        }
                    } else {
                        Button(onClick = { isStreaming = false }) { Text("Stop stream") }
                        MjpegStreamingView(
                            url = streamUrl,
                            onError = { msg ->
                                streamError = msg
                                isStreaming = false
                            },
                            onStop = { isStreaming = false },
                        )
                    }
                }

                HorizontalDivider()
                Text("Vision metadata overlay", style = MaterialTheme.typography.titleMedium)
                Button(onClick = { overlayEnabled = !overlayEnabled }) {
                    Text(if (overlayEnabled) "Disable overlay" else "Enable overlay")
                }

                if (overlayEnabled) {
                    val det = state.telemetry?.vision_last_detection
                    if (det == null) {
                        Text("No detection available")
                    } else {
                        Text("Label: ${confidenceLabel(det.label, state.lastTelemetryAt)}")
                        Text("Confidence: ${confidenceLabel(det.confidence, state.lastTelemetryAt)}")
                        Text("BBox: ${confidenceLabel(det.bbox?.joinToString(prefix = "[", postfix = "]"), state.lastTelemetryAt)}")
                        Text("Timestamp: ${confidenceLabel(det.ts, state.lastTelemetryAt)}")
                    }
                } else {
                    Text("Overlay is off")
                }

                HorizontalDivider()
                Text("Capture", style = MaterialTheme.typography.titleMedium)
                Button(onClick = { viewModel.sendIntent("capture_frame") }, enabled = intentsEnabled) {
                    Text("Capture frame")
                }

                HorizontalDivider()
                Text("Detection history", style = MaterialTheme.typography.titleMedium)
                val history = state.telemetry?.detection_history.orEmpty()
                if (history.isEmpty()) {
                    Text("No detections yet")
                } else {
                    history.takeLast(10).reversed().forEach { item ->
                        Text(
                            "${item.label ?: "unknown"} " +
                                "conf=${item.confidence ?: "?"} " +
                                "bbox=${item.bbox?.joinToString(prefix = "[", postfix = "]") ?: "[]"}"
                        )
                    }
                }
            }
        }
        Spacer(modifier = Modifier.height(12.dp))
    }
}

@Composable
private fun SettingsScreen(
    modifier: Modifier,
    viewModel: AppViewModel,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    var ip by rememberSaveable { mutableStateOf(state.settings?.robotIp ?: "") }
    var port by rememberSaveable { mutableStateOf(state.settings?.robotPort?.toString() ?: "") }
    var pollMs by rememberSaveable { mutableStateOf(state.settings?.pollIntervalMs?.toString() ?: "1000") }
    var debugEnabled by rememberSaveable { mutableStateOf(state.settings?.debugEnabled ?: false) }
    var errorMessage by rememberSaveable { mutableStateOf<String?>(null) }

    LaunchedEffect(state.settings) {
        val settings = state.settings
        if (settings != null) {
            ip = settings.robotIp
            port = settings.robotPort.toString()
            pollMs = settings.pollIntervalMs.toString()
            debugEnabled = settings.debugEnabled
        }
    }

    Column(
        modifier = modifier
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Card {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Connection settings", style = MaterialTheme.typography.titleMedium)
                OutlinedTextField(
                    value = ip,
                    onValueChange = { ip = it.trim() },
                    label = { Text("Robot IP") },
                    singleLine = true,
                )
                OutlinedTextField(
                    value = port,
                    onValueChange = { port = it.trim() },
                    label = { Text("Port") },
                    singleLine = true,
                )
                OutlinedTextField(
                    value = pollMs,
                    onValueChange = { pollMs = it.trim() },
                    label = { Text("Poll interval (ms)") },
                    singleLine = true,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Debug panel")
                    Switch(checked = debugEnabled, onCheckedChange = { debugEnabled = it })
                }
                Text(
                    "Current base URL: ${state.settings?.baseUrl() ?: BuildConfig.ROBOT_BASE_URL}",
                    style = MaterialTheme.typography.bodySmall,
                )
                if (errorMessage != null) {
                    Text(errorMessage!!, color = MaterialTheme.colorScheme.error)
                }
                Button(onClick = {
                    val parsedPort = port.toIntOrNull()
                    val parsedPoll = pollMs.toLongOrNull()
                    val ipValue = ip.trim()
                    errorMessage = when {
                        ipValue.isBlank() -> "IP address is required"
                        parsedPort == null || parsedPort <= 0 -> "Port must be a positive integer"
                        parsedPoll == null || parsedPoll < 250L -> "Poll interval must be at least 250ms"
                        else -> null
                    }
                    if (errorMessage == null) {
                        viewModel.updateSettings(
                            AppSettings(
                                robotIp = ipValue,
                                robotPort = parsedPort!!,
                                pollIntervalMs = parsedPoll!!,
                                debugEnabled = debugEnabled,
                            )
                        )
                    }
                }) {
                    Text("Apply settings")
                }
            }
        }
        Spacer(modifier = Modifier.height(12.dp))
    }
}

@Composable
private fun MjpegStreamingView(
    url: String,
    onError: (String) -> Unit,
    onStop: () -> Unit,
) {
    val context = LocalContext.current
    val client = remember { OkHttpClient.Builder().build() }
    var latestBitmap by remember { mutableStateOf<Bitmap?>(null) }
    val scope = rememberCoroutineScope()

    DisposableEffect(url) {
        val job = scope.launch(Dispatchers.IO) {
            try {
                val request = Request.Builder().url(url).build()
                val response = client.newCall(request).execute()
                if (!response.isSuccessful) {
                    withContext(Dispatchers.Main) { onError("http_${response.code}") }
                    response.close()
                    return@launch
                }
                val contentType = response.header("Content-Type")
                val boundary = parseBoundary(contentType)
                val body = response.body ?: run {
                    withContext(Dispatchers.Main) { onError("empty_body") }
                    response.close()
                    return@launch
                }
                val source = body.source()
                while (isActive) {
                    val line = source.readUtf8Line() ?: break
                    if (!line.startsWith(boundary)) {
                        continue
                    }
                    var contentLength = -1
                    while (true) {
                        val header = source.readUtf8Line() ?: break
                        if (header.isBlank()) break
                        val parts = header.split(":", limit = 2)
                        if (parts.size == 2 && parts[0].trim().equals("Content-Length", true)) {
                            contentLength = parts[1].trim().toIntOrNull() ?: -1
                        }
                    }
                    if (contentLength <= 0) {
                        continue
                    }
                    val bytes = source.readByteArray(contentLength.toLong())
                    val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                    if (bitmap != null) {
                        withContext(Dispatchers.Main) { latestBitmap = bitmap }
                    }
                    source.readUtf8Line()
                }
                response.close()
            } catch (err: CancellationException) {
                // Ignore cancellation
            } catch (err: Exception) {
                withContext(Dispatchers.Main) { onError(err.message ?: "stream_error") }
            }
        }

        onDispose {
            job.cancel()
            onStop()
        }
    }

    AndroidView(
        factory = {
            val view = ImageView(context)
            view.layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
            view.adjustViewBounds = true
            view
        },
        update = { view ->
            view.setImageBitmap(latestBitmap)
        },
        modifier = Modifier.padding(top = 8.dp)
    )
}

private fun triState(value: Boolean?): String = when (value) {
    null -> "unknown"
    true -> "true"
    false -> "false"
}

private fun confidenceLabel(value: Any?, lastTs: Long?): String {
    val now = System.currentTimeMillis()
    val freshness = if (lastTs == null) "UNKNOWN" else if (now - lastTs > 5_000) "STALE" else "LIVE"
    val v = value?.toString() ?: "unknown"
    return "$v ($freshness)"
}

private fun resolveStreamUrl(raw: String?, settings: AppSettings?): String {
    val value = raw?.trim().orEmpty()
    if (value.isBlank()) return ""
    if (value.startsWith("http://") || value.startsWith("https://")) return value
    val base = settings?.baseUrl() ?: BuildConfig.ROBOT_BASE_URL
    return base.trimEnd('/') + "/" + value.trimStart('/')
}

private fun parseBoundary(contentType: String?): String {
    if (contentType.isNullOrBlank()) return "--frame"
    val boundary = contentType.split(";")
        .map { it.trim() }
        .firstOrNull { it.startsWith("boundary=") }
        ?.substringAfter("boundary=")
        ?.trim('"')
        ?: "frame"
    return if (boundary.startsWith("--")) boundary else "--$boundary"
}

private fun formatTimestamp(ts: Long?): String {
    if (ts == null) return "unknown"
    val formatter = DateTimeFormatter.ofPattern("HH:mm:ss")
    return Instant.ofEpochMilli(ts).atZone(ZoneId.systemDefault()).format(formatter)
}
