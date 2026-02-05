package com.smartcar.supervision.ui.screens

import android.graphics.BitmapFactory
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.smartcar.supervision.BuildConfig
import com.smartcar.supervision.data.AppSettings
import com.smartcar.supervision.ui.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit

// =============================================================================
// TAB ENUM
// =============================================================================
enum class MainTab(val label: String, val icon: @Composable () -> Unit) {
    HOME("Home", { Icon(Icons.Default.Home, contentDescription = "Home") }),
    TASK("Task", { Icon(Icons.Default.Assignment, contentDescription = "Task") }),
    CONTROL("Control", { Icon(Icons.Default.Gamepad, contentDescription = "Control") }),
    VISION("Vision", { Icon(Icons.Default.RemoveRedEye, contentDescription = "Vision") }),
    SENSORS("Sensors", { Icon(Icons.Default.Sensors, contentDescription = "Sensors") }),
    LOGS("Logs", { Icon(Icons.Default.Article, contentDescription = "Logs") }),
    SETTINGS("Settings", { Icon(Icons.Default.Settings, contentDescription = "Settings") }),
}

// =============================================================================
// MAIN SCREEN COMPOSABLE - OPERATOR-FIRST DESIGN
// =============================================================================
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(viewModel: AppViewModel) {
    val context = LocalContext.current
    LaunchedEffect(Unit) { viewModel.bindContext(context) }

    val state by viewModel.state.collectAsState()
    var selectedTab by remember { mutableStateOf(MainTab.HOME) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("SmartCar", fontWeight = FontWeight.Bold)
                        Spacer(Modifier.width(8.dp))
                        ConnectionIndicator(state.connection, state.appStatus)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.primaryContainer,
                )
            )
        },
        bottomBar = {
            StopCenterNavigationBar(
                selectedTab = selectedTab,
                onTabSelected = { selectedTab = it },
                onStop = {
                    viewModel.sendIntent("stop")
                    viewModel.logUiAction("stop_pressed", true, null)
                }
            )
        }
    ) { padding ->
        Box(modifier = Modifier.padding(padding)) {
            when (selectedTab) {
                MainTab.HOME -> HomeScreen(state, viewModel)
                MainTab.TASK -> TaskScreen(state)
                MainTab.CONTROL -> ControlScreen(state, viewModel)
                MainTab.VISION -> VisionScreen(state, viewModel)
                MainTab.SENSORS -> SensorsScreen(state)
                MainTab.LOGS -> LogsScreen(state, viewModel)
                MainTab.SETTINGS -> SettingsScreen(state, viewModel)
            }
        }
    }
}

// =============================================================================
// CONNECTION INDICATOR
// =============================================================================
@Composable
fun ConnectionIndicator(connection: ConnectionStatus, appStatus: AppStatus) {
    val (color, label) = when (connection) {
        ConnectionStatus.Online -> when (appStatus) {
            AppStatus.ONLINE_IDLE -> Color(0xFF4CAF50) to "Ready"
            AppStatus.ONLINE_BUSY -> Color(0xFFFF9800) to "Busy"
            AppStatus.ONLINE_EXECUTING_TASK -> Color(0xFFFF9800) to "Task"
            else -> Color(0xFF4CAF50) to "Online"
        }
        ConnectionStatus.Offline -> Color(0xFF9E9E9E) to "Offline"
        is ConnectionStatus.Error -> Color(0xFFF44336) to "Error"
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(
            modifier = Modifier
                .size(10.dp)
                .clip(CircleShape)
                .background(color)
        )
        Spacer(Modifier.width(4.dp))
        Text(label, fontSize = 12.sp, color = MaterialTheme.colorScheme.onPrimaryContainer)
    }
}

// =============================================================================
// STOP-CENTER NAVIGATION BAR
// =============================================================================
@Composable
fun StopCenterNavigationBar(
    selectedTab: MainTab,
    onTabSelected: (MainTab) -> Unit,
    onStop: () -> Unit,
) {
    Surface(tonalElevation = 3.dp) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 6.dp, horizontal = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            val leftTabs = listOf(MainTab.HOME, MainTab.TASK, MainTab.CONTROL)
            val rightTabs = listOf(MainTab.VISION, MainTab.SENSORS, MainTab.LOGS, MainTab.SETTINGS)

            Row(modifier = Modifier.weight(1f), horizontalArrangement = Arrangement.SpaceEvenly) {
                leftTabs.forEach { tab ->
                    NavItem(tab = tab, selected = selectedTab == tab, onClick = { onTabSelected(tab) })
                }
            }

            Box(
                modifier = Modifier
                    .size(64.dp)
                    .clip(CircleShape)
                    .background(Color(0xFFD32F2F))
                    .clickable { onStop() },
                contentAlignment = Alignment.Center
            ) {
                Icon(
                    Icons.Default.Stop,
                    contentDescription = "Emergency Stop",
                    tint = Color.White,
                    modifier = Modifier.size(32.dp)
                )
            }

            Row(modifier = Modifier.weight(1f), horizontalArrangement = Arrangement.SpaceEvenly) {
                rightTabs.forEach { tab ->
                    NavItem(tab = tab, selected = selectedTab == tab, onClick = { onTabSelected(tab) })
                }
            }
        }
    }
}

@Composable
fun NavItem(tab: MainTab, selected: Boolean, onClick: () -> Unit) {
    val color = if (selected) MaterialTheme.colorScheme.primary else Color.Gray
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier
            .padding(horizontal = 2.dp, vertical = 4.dp)
            .clickable { onClick() }
    ) {
        CompositionLocalProvider(LocalContentColor provides color) {
            tab.icon()
        }
        Text(tab.label, fontSize = 10.sp, color = color)
    }
}

// =============================================================================
// HOME SCREEN - READ-ONLY STATUS DISPLAY
// =============================================================================
@Composable
fun HomeScreen(state: AppState, viewModel: AppViewModel) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text("System Status", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(16.dp))

        // Connection Status Card
        StatusCard("Connection") {
            val connText = when (state.connection) {
                ConnectionStatus.Online -> "Online"
                ConnectionStatus.Offline -> "Offline"
                is ConnectionStatus.Error -> "Error: ${state.connection.message}"
            }
            StatusRow("Status", connText)
            StatusRow("App Status", state.appStatus.name)
            state.blockingReason?.let { StatusRow("Blocking", it) }
            state.lastConnectAttemptAt?.let { StatusRow("Last Attempt", formatTime(it)) }
        }

        Spacer(Modifier.height(12.dp))

        // Robot Status Card
        StatusCard("Robot State") {
            state.telemetry?.let { t ->
                StatusRow("Mode", t.mode ?: "-")
                StatusRow("Remote Session", if (t.remote_session_active == true) "Active" else "Inactive")
                StatusRow("Motor Enabled", if (t.motor_enabled == true) "Yes" else "No")
                StatusRow("Safety Stop", if (t.safety_stop == true) "ACTIVE" else "Clear")
                t.safety_alert?.let { StatusRow("Safety Alert", it) }
                StatusRow("Vision Active", if (t.vision_active == true) "Yes" else "No")
                t.vision_mode?.let { StatusRow("Vision Mode", it) }
            } ?: Text("No telemetry data", color = Color.Gray)
        }

        Spacer(Modifier.height(12.dp))

        // Last Activity Card
        StatusCard("Recent Activity") {
            state.lastIntentSent?.let { StatusRow("Last Intent", it) }
            state.lastIntentResult?.let { StatusRow("Result", it) }
            state.lastIntentAt?.let { StatusRow("Sent At", formatTime(it)) }
            state.lastRemoteEvent?.let { StatusRow("Last Event", it) }
            if (state.task.type != TaskType.NONE) {
                StatusRow("Task", "${state.task.type} - ${state.task.phase}")
                StatusRow("Task Label", state.task.label)
            }
        }

        Spacer(Modifier.height(12.dp))

        // LLM/TTS Status
        state.telemetry?.let { t ->
            StatusCard("AI / Voice") {
                t.last_llm_response?.let { StatusRow("LLM Response", it.take(100) + if (it.length > 100) "..." else "") }
                t.last_tts_text?.let { StatusRow("TTS Text", it.take(50) + if (it.length > 50) "..." else "") }
                t.last_tts_status?.let { StatusRow("TTS Status", it) }
                t.last_scan_summary?.let { StatusRow("Scan Summary", it.take(100) + if (it.length > 100) "..." else "") }
            }
        }

        Spacer(Modifier.height(12.dp))

        // Health Status
        StatusCard("Health") {
            state.health?.let { h ->
                StatusRow("Health OK", if (h.ok == true) "Yes" else "No")
                h.timestamp?.let { StatusRow("Checked At", formatTime(it * 1000)) }
            } ?: Text("No health data", color = Color.Gray)
        }
    }
}

@Composable
fun StatusCard(title: String, content: @Composable ColumnScope.() -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text(title, fontWeight = FontWeight.Bold, fontSize = 14.sp)
            Spacer(Modifier.height(8.dp))
            content()
        }
    }
}

@Composable
fun StatusRow(label: String, value: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(label, fontSize = 12.sp, color = Color.Gray)
        Text(value, fontSize = 12.sp, fontWeight = FontWeight.Medium)
    }
}

@Composable
fun StatusRowColored(label: String, value: String, valueColor: Color) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(label, fontSize = 12.sp, color = Color.Gray)
        Text(value, fontSize = 12.sp, fontWeight = FontWeight.Medium, color = valueColor)
    }
}

// =============================================================================
// CONTROL SCREEN - WITH EMBEDDED STREAM (NON-NEGOTIABLE)
// =============================================================================
@Composable
fun ControlScreen(state: AppState, viewModel: AppViewModel) {
    val baseUrl = state.settings?.baseUrl() ?: BuildConfig.ROBOT_BASE_URL
    val streamUrl = baseUrl.trimEnd('/') + "/stream/mjpeg"
    val streamOwner = state.streamOwner
    val streamEnabled = streamOwner == StreamOwner.CONTROL
    val streamLockedByOther = streamOwner != null && streamOwner != StreamOwner.CONTROL

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        // Stream Toggle
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Live Stream", fontWeight = FontWeight.Bold)
            Switch(
                checked = streamEnabled,
                onCheckedChange = { 
                    val allowed = viewModel.requestStream(StreamOwner.CONTROL, it)
                    val blockedReason = if (!allowed) "stream_in_use" else null
                    viewModel.logUiAction("stream_toggle", allowed, blockedReason)
                }
            )
        }

        if (streamLockedByOther) {
            Spacer(Modifier.height(4.dp))
            Text("Stream already active in Vision screen", color = Color.Red, fontSize = 12.sp)
        }

        // MJPEG Stream View - EMBEDDED IN CONTROL (NON-NEGOTIABLE)
        if (streamEnabled) {
            Spacer(Modifier.height(8.dp))
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(16f / 9f),
                colors = CardDefaults.cardColors(containerColor = Color.Black)
            ) {
                MjpegStreamView(
                    url = streamUrl,
                    modifier = Modifier.fillMaxSize()
                )
            }
        }

        Spacer(Modifier.height(16.dp))

        // CONTROL BUTTONS - ALWAYS VISIBLE, ALWAYS ENABLED, ALWAYS RESPONSIVE
        Text("Movement Controls", fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(8.dp))

        val canControl = state.connection == ConnectionStatus.Online &&
            state.telemetry?.remote_session_active == true &&
            !state.intentInFlight

        // Control Grid
        Column(
            modifier = Modifier.fillMaxWidth(),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            // Forward Button
            ControlButton(
                label = "FORWARD",
                icon = Icons.Default.KeyboardArrowUp,
                enabled = canControl,
                onClick = {
                    viewModel.logUiAction("forward", canControl, if (!canControl) "control_disabled" else null)
                    if (canControl) viewModel.sendIntent("start_motion", direction = "forward")
                }
            )
            
            Spacer(Modifier.height(8.dp))

            // Left - Right Row (STOP is always in bottom nav)
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceEvenly
            ) {
                ControlButton(
                    label = "LEFT",
                    icon = Icons.AutoMirrored.Filled.ArrowBack,
                    enabled = canControl,
                    onClick = {
                        viewModel.logUiAction("rotate_left", canControl, if (!canControl) "control_disabled" else null)
                        if (canControl) viewModel.sendIntent("rotate_left", direction = "left")
                    }
                )
                
                ControlButton(
                    label = "RIGHT",
                    icon = Icons.AutoMirrored.Filled.ArrowForward,
                    enabled = canControl,
                    onClick = {
                        viewModel.logUiAction("rotate_right", canControl, if (!canControl) "control_disabled" else null)
                        if (canControl) viewModel.sendIntent("rotate_right", direction = "right")
                    }
                )
            }
            
            Spacer(Modifier.height(8.dp))

            // Backward Button
            ControlButton(
                label = "BACKWARD",
                icon = Icons.Default.KeyboardArrowDown,
                enabled = canControl,
                onClick = {
                    viewModel.logUiAction("backward", canControl, if (!canControl) "control_disabled" else null)
                    if (canControl) viewModel.sendIntent("move_backward", direction = "backward")
                }
            )
        }

        Spacer(Modifier.height(16.dp))

        // Additional Actions
        Text("Actions", fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(8.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceEvenly
        ) {
            ActionButton("Scan", Icons.Default.Search, canControl) {
                viewModel.logUiAction("scan", canControl, null)
                if (canControl) viewModel.startScanObserveTask()
            }
            ActionButton("Observe", Icons.Default.Visibility, canControl) {
                viewModel.logUiAction("observe", canControl, null)
                if (canControl) viewModel.markObservation()
            }
        }

        Spacer(Modifier.height(8.dp))

        Spacer(Modifier.height(16.dp))

        // Scan Summary (last reported)
        StatusCard("Last Scan Summary") {
            val summary = state.telemetry?.last_scan_summary
            Text(summary ?: "No scan summary yet", fontSize = 12.sp, color = if (summary == null) Color.Gray else Color.Unspecified)
        }

        Spacer(Modifier.height(16.dp))

        // Assistant Control - RESTORED
        Text("Assistant", fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(8.dp))

        var assistantText by remember { mutableStateOf("") }

        Button(
            onClick = {
                viewModel.logUiAction("invoke_assistant", canControl, null)
                if (canControl) viewModel.sendIntent("invoke_assistant")
            },
            enabled = canControl,
            modifier = Modifier.fillMaxWidth()
        ) {
            Icon(Icons.Default.RecordVoiceOver, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Invoke Assistant")
        }

        Spacer(Modifier.height(8.dp))

        OutlinedTextField(
            value = assistantText,
            onValueChange = { assistantText = it },
            label = { Text("Custom message") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = false,
            maxLines = 3
        )

        Spacer(Modifier.height(8.dp))

        Button(
            onClick = {
                viewModel.logUiAction("assistant_text", canControl, null)
                if (canControl && assistantText.isNotBlank()) {
                    viewModel.sendIntent("assistant_text", text = assistantText.trim())
                    assistantText = ""
                }
            },
            enabled = canControl && assistantText.isNotBlank(),
            modifier = Modifier.fillMaxWidth()
        ) {
            Icon(Icons.Default.Send, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Send to Assistant")
        }

        // Failure Surface - RESTORED
        val sessionActive = state.telemetry?.remote_session_active == true || state.status?.remote_session_active == true
        val hasFailure = state.connection is ConnectionStatus.Error || !sessionActive ||
            state.lastIntentResult?.startsWith("rejected") == true ||
            state.lastIntentResult?.startsWith("timed_out") == true ||
            state.lastIntentResult?.startsWith("failed") == true

        if (hasFailure) {
            Spacer(Modifier.height(12.dp))
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text("Failure Surface", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Spacer(Modifier.height(4.dp))
                    if (state.connection is ConnectionStatus.Error) {
                        Text("Network: ${(state.connection as ConnectionStatus.Error).message}", fontSize = 12.sp)
                    }
                    if (!sessionActive) {
                        Text("Session: Remote session inactive", fontSize = 12.sp)
                    }
                    state.lastIntentResult?.let { result ->
                        if (result.startsWith("rejected") || result.startsWith("timed_out") || result.startsWith("failed")) {
                            Text("Intent: $result", fontSize = 12.sp)
                        }
                    }
                }
            }
        }

        // Control Status Info
        if (!canControl) {
            Spacer(Modifier.height(12.dp))
            Card(
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)
            ) {
                Text(
                    text = when {
                        state.connection != ConnectionStatus.Online -> "Controls disabled: Not connected"
                        state.telemetry?.remote_session_active != true -> "Controls disabled: No remote session"
                        state.intentInFlight -> "Controls disabled: Intent in progress"
                        else -> "Controls disabled"
                    },
                    modifier = Modifier.padding(12.dp),
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onErrorContainer
                )
            }
        }
    }
}

// =============================================================================
// TASK SCREEN - READ-ONLY OBSERVATION
// =============================================================================
@Composable
fun TaskScreen(state: AppState) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text("Task Monitor", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(16.dp))

        StatusCard("Task State") {
            StatusRow("Task Type", state.task.type.name)
            StatusRow("Phase", state.task.phase.name)
            StatusRow("Label", state.task.label.ifBlank { "-" })
            state.lastRemoteEvent?.let { StatusRow("Last Remote Event", it) }
            state.lastIntentSent?.let { StatusRow("Last Intent", it) }
            state.lastIntentResult?.let { StatusRow("Last Result", it) }
        }

        Spacer(Modifier.height(12.dp))

        StatusCard("Scan Summary") {
            val summary = state.telemetry?.last_scan_summary
            Text(summary ?: "No scan summary yet", fontSize = 12.sp, color = if (summary == null) Color.Gray else Color.Unspecified)
        }

        Spacer(Modifier.height(12.dp))

        StatusCard("LLM Response") {
            val llm = state.telemetry?.last_llm_response
            Text(llm ?: "No LLM response yet", fontSize = 12.sp, color = if (llm == null) Color.Gray else Color.Unspecified)
        }

        Spacer(Modifier.height(12.dp))

        val taskEvents = state.logs.filter { entry ->
            entry.category == LogCategory.STATE && (
                entry.event.contains("task") ||
                entry.event.contains("remote_event") ||
                entry.event.contains("scan")
            )
        }.takeLast(20).reversed()

        StatusCard("Task / Scan Events") {
            if (taskEvents.isEmpty()) {
                Text("No task events", fontSize = 12.sp, color = Color.Gray)
            } else {
                Column(modifier = Modifier.heightIn(max = 240.dp)) {
                    taskEvents.forEach { entry ->
                        Text(entry.toDisplayLine(), fontSize = 10.sp, fontFamily = FontFamily.Monospace)
                        Spacer(Modifier.height(2.dp))
                    }
                }
            }
        }

        Spacer(Modifier.height(12.dp))

        state.telemetry?.detection_history?.takeIf { it.isNotEmpty() }?.let { history ->
            Text("Detection History", fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(8.dp))
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 200.dp)
            ) {
                history.takeLast(10).reversed().forEach { det ->
                    Card(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = 2.dp),
                        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
                    ) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(8.dp),
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text(det.label ?: "Unknown", fontSize = 12.sp, fontWeight = FontWeight.Medium)
                            Text("${((det.confidence ?: 0.0) * 100).toInt()}%", fontSize = 12.sp, color = Color.Gray)
                        }
                    }
                }
            }
        } ?: StatusCard("Detection History") {
            Text("No detections", fontSize = 12.sp, color = Color.Gray)
        }
    }
}

@Composable
fun ControlButton(
    label: String,
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    enabled: Boolean,
    onClick: () -> Unit
) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = Modifier.size(80.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = MaterialTheme.colorScheme.primary,
            disabledContainerColor = Color.Gray.copy(alpha = 0.3f)
        )
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(icon, contentDescription = label, modifier = Modifier.size(24.dp))
            Text(label, fontSize = 8.sp)
        }
    }
}

@Composable
fun ActionButton(
    label: String,
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    enabled: Boolean,
    onClick: () -> Unit
) {
    OutlinedButton(
        onClick = onClick,
        enabled = enabled,
    ) {
        Icon(icon, contentDescription = label, modifier = Modifier.size(16.dp))
        Spacer(Modifier.width(4.dp))
        Text(label, fontSize = 12.sp)
    }
}

// =============================================================================
// MJPEG STREAM VIEW COMPOSABLE
// =============================================================================
@Composable
fun MjpegStreamView(url: String, modifier: Modifier = Modifier) {
    var bitmap by remember { mutableStateOf<android.graphics.Bitmap?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(url) {
        withContext(Dispatchers.IO) {
            try {
                val client = OkHttpClient.Builder()
                    .connectTimeout(10, TimeUnit.SECONDS)
                    .readTimeout(30, TimeUnit.SECONDS)
                    .build()

                val request = Request.Builder()
                    .url(url)
                    .header("Accept", "multipart/x-mixed-replace")
                    .build()

                val response = client.newCall(request).execute()
                if (!response.isSuccessful) {
                    error = "Stream error: ${response.code}"
                    return@withContext
                }

                val body = response.body ?: run {
                    error = "Empty response"
                    return@withContext
                }

                val inputStream = BufferedInputStream(body.byteStream())
                val boundaryPattern = "--".toByteArray()
                val buffer = ByteArrayOutputStream()

                var inImage = false
                var headersParsed = false
                val headerBuffer = StringBuilder()

                while (isActive) {
                    val b = inputStream.read()
                    if (b == -1) break

                    if (!inImage) {
                        headerBuffer.append(b.toChar())
                        val headers = headerBuffer.toString()
                        if (headers.contains("\r\n\r\n") || headers.contains("\n\n")) {
                            if (headers.contains("Content-Type: image/jpeg", ignoreCase = true)) {
                                inImage = true
                                headersParsed = true
                                buffer.reset()
                            }
                            headerBuffer.clear()
                        }
                    } else {
                        buffer.write(b)
                        val data = buffer.toByteArray()
                        
                        // Check for JPEG end marker (FFD9)
                        if (data.size >= 2 && 
                            data[data.size - 2] == 0xFF.toByte() && 
                            data[data.size - 1] == 0xD9.toByte()) {
                            
                            // Decode frame
                            try {
                                val decoded = BitmapFactory.decodeByteArray(data, 0, data.size)
                                if (decoded != null) {
                                    withContext(Dispatchers.Main) {
                                        bitmap = decoded
                                        error = null
                                    }
                                }
                            } catch (e: Exception) {
                                // Skip bad frame
                            }
                            
                            buffer.reset()
                            inImage = false
                        }
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    error = "Stream error: ${e.message}"
                }
            }
        }
    }

    Box(
        modifier = modifier,
        contentAlignment = Alignment.Center
    ) {
        when {
            error != null -> {
                Text(
                    text = error ?: "Unknown error",
                    color = Color.Red,
                    fontSize = 12.sp,
                    textAlign = TextAlign.Center
                )
            }
            bitmap != null -> {
                Image(
                    bitmap = bitmap!!.asImageBitmap(),
                    contentDescription = "Live Stream",
                    modifier = Modifier.fillMaxSize(),
                    contentScale = ContentScale.Fit
                )
            }
            else -> {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    CircularProgressIndicator(color = Color.White, modifier = Modifier.size(24.dp))
                    Spacer(Modifier.height(8.dp))
                    Text("Connecting...", color = Color.White, fontSize = 12.sp)
                }
            }
        }
    }
}

// =============================================================================
// VISION SCREEN - DEDICATED VISION VIEW
// =============================================================================
@Composable
fun VisionScreen(state: AppState, viewModel: AppViewModel) {
    val baseUrl = state.settings?.baseUrl() ?: BuildConfig.ROBOT_BASE_URL
    val streamUrl = baseUrl.trimEnd('/') + "/stream/mjpeg"
    val streamOwner = state.streamOwner
    val streamEnabled = streamOwner == StreamOwner.VISION
    val streamLockedByOther = streamOwner != null && streamOwner != StreamOwner.VISION
    var overlayEnabled by remember { mutableStateOf(false) }

    val intentsEnabled = state.appStatus == AppStatus.ONLINE_IDLE
    val sessionActive = state.telemetry?.remote_session_active == true || state.status?.remote_session_active == true
    val canControl = state.connection == ConnectionStatus.Online && sessionActive && !state.intentInFlight

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text("Vision System", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(16.dp))

        // Vision Control - RESTORED
        StatusCard("Vision Control") {
            val visionMode = state.telemetry?.vision_mode ?: state.status?.vision_mode
            val visionModeLabel = when (visionMode) {
                "off" -> "OFF"
                "on" -> "ON (no stream)"
                "on_with_stream" -> "ON + STREAM"
                null -> "UNKNOWN"
                else -> visionMode.uppercase()
            }
            StatusRow("Vision Mode", visionModeLabel)
            
            Spacer(Modifier.height(8.dp))
            
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Button(
                    onClick = {
                        viewModel.logUiAction("enable_vision", canControl, null)
                        if (canControl) viewModel.sendIntent("enable_vision")
                    },
                    enabled = canControl,
                    modifier = Modifier.weight(1f)
                ) { Text("Vision ON") }
                Button(
                    onClick = {
                        viewModel.logUiAction("disable_vision", canControl, null)
                        if (canControl) viewModel.sendIntent("disable_vision")
                    },
                    enabled = canControl,
                    modifier = Modifier.weight(1f)
                ) { Text("Vision OFF") }
            }
            
            Spacer(Modifier.height(8.dp))
            
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Button(
                    onClick = {
                        viewModel.logUiAction("enable_stream", canControl, null)
                        if (canControl) viewModel.sendIntent("enable_stream")
                    },
                    enabled = canControl,
                    modifier = Modifier.weight(1f)
                ) { Text("Stream ON") }
                Button(
                    onClick = {
                        viewModel.logUiAction("disable_stream", canControl, null)
                        if (canControl) viewModel.sendIntent("disable_stream")
                    },
                    enabled = canControl,
                    modifier = Modifier.weight(1f)
                ) { Text("Stream OFF") }
            }
        }

        Spacer(Modifier.height(12.dp))

        // Stream View Toggle (local)
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Show Stream View", fontWeight = FontWeight.Bold)
            Switch(
                checked = streamEnabled,
                onCheckedChange = {
                    val allowed = viewModel.requestStream(StreamOwner.VISION, it)
                    val blockedReason = if (!allowed) "stream_in_use" else null
                    viewModel.logUiAction("vision_stream_view_toggle", allowed, blockedReason)
                }
            )
        }

        if (streamLockedByOther) {
            Spacer(Modifier.height(4.dp))
            Text("Stream already active in Control screen", color = Color.Red, fontSize = 12.sp)
        }

        if (streamEnabled) {
            Spacer(Modifier.height(8.dp))
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(16f / 9f),
                colors = CardDefaults.cardColors(containerColor = Color.Black)
            ) {
                MjpegStreamView(
                    url = streamUrl,
                    modifier = Modifier.fillMaxSize()
                )
            }
        }

        Spacer(Modifier.height(12.dp))

        // Capture Frame - RESTORED
        Button(
            onClick = {
                viewModel.logUiAction("capture_frame", canControl, null)
                if (canControl) viewModel.sendIntent("capture_frame")
            },
            enabled = canControl,
            modifier = Modifier.fillMaxWidth()
        ) {
            Icon(Icons.Default.Camera, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Capture Frame")
        }

        if (state.lastIntentSent == "capture_frame") {
            Text(
                "Capture: ${state.lastIntentResult ?: "pending"} @ ${state.lastIntentAt?.let { formatTime(it) } ?: "-"}",
                fontSize = 10.sp,
                color = Color.Gray
            )
        }

        Spacer(Modifier.height(12.dp))

        // Vision Status
        StatusCard("Vision Status") {
            state.telemetry?.let { t ->
                StatusRow("Vision Active", if (t.vision_active == true) "Yes" else "No")
                StatusRow("Vision Paused", if (t.vision_paused == true) "Yes" else "No")
                t.vision_mode?.let { StatusRow("Mode", it) }
                t.stream_url?.let { StatusRow("Stream URL", it) }
            } ?: Text("No vision data", color = Color.Gray)
        }

        Spacer(Modifier.height(12.dp))

        // Overlay Toggle - RESTORED
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Detection Overlay", fontWeight = FontWeight.Bold)
            Switch(
                checked = overlayEnabled,
                onCheckedChange = { overlayEnabled = it }
            )
        }

        // Last Detection (shown when overlay enabled or always)
        if (overlayEnabled) {
            Spacer(Modifier.height(8.dp))
            StatusCard("Last Detection (Overlay)") {
                state.telemetry?.vision_last_detection?.let { det ->
                    StatusRow("Label", det.label ?: "-")
                    det.confidence?.let { StatusRow("Confidence", "%.2f%%".format(it * 100)) }
                    det.bbox?.let { StatusRow("BBox", it.joinToString(", ")) }
                    det.ts?.let { StatusRow("Timestamp", "%.2f".format(it)) }
                } ?: Text("No detections", color = Color.Gray)
            }
        }

        Spacer(Modifier.height(12.dp))

        // Detection History
        state.telemetry?.detection_history?.takeIf { it.isNotEmpty() }?.let { history ->
            Text("Detection History", fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(8.dp))
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 200.dp)
            ) {
                history.takeLast(10).reversed().forEach { det ->
                    Card(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = 2.dp),
                        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
                    ) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(8.dp),
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text(det.label ?: "Unknown", fontSize = 12.sp, fontWeight = FontWeight.Medium)
                            Text("${((det.confidence ?: 0.0) * 100).toInt()}%", fontSize = 12.sp, color = Color.Gray)
                        }
                    }
                }
            }
        }
    }
}

// =============================================================================
// SENSORS SCREEN - SENSOR DATA DISPLAY
// =============================================================================
@Composable
fun SensorsScreen(state: AppState) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text("Sensors", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(16.dp))

        // Distance Sensors
        StatusCard("Distance Sensors") {
            state.telemetry?.sensor?.let { s ->
                SensorBar("Sensor 1 (Left)", s.s1, 400)
                SensorBar("Sensor 2 (Center)", s.s2, 400)
                SensorBar("Sensor 3 (Right)", s.s3, 400)
                Spacer(Modifier.height(8.dp))
                s.min_distance?.let { StatusRow("Min Distance", "$it cm") }
                StatusRow("Obstacle", if (s.obstacle == true) "YES" else "Clear")
                StatusRow("Warning", if (s.warning == true) "ACTIVE" else "None")
                StatusRow("Is Safe", if (s.is_safe == true) "Yes" else "NO")
            } ?: Text("No sensor data", color = Color.Gray)
        }

        Spacer(Modifier.height(12.dp))

        // Motor State
        StatusCard("Motor State") {
            state.telemetry?.let { t ->
                StatusRow("Motor Enabled", if (t.motor_enabled == true) "Yes" else "No")
                t.motor?.let { m ->
                    StatusRow("Left Motor", "${m.left ?: 0}")
                    StatusRow("Right Motor", "${m.right ?: 0}")
                }
                t.sensor?.let { s ->
                    StatusRow("L Motor (Raw)", "${s.lmotor ?: 0}")
                    StatusRow("R Motor (Raw)", "${s.rmotor ?: 0}")
                }
            } ?: Text("No motor data", color = Color.Gray)
        }

        Spacer(Modifier.height(12.dp))

        // Gas Sensor
        StatusCard("Gas Sensor (MQ2)") {
            state.telemetry?.let { t ->
                t.sensor?.mq2?.let { SensorBar("MQ2 Level", it, 1000) }
                t.gas_level?.let { StatusRow("Gas Level", "$it") }
                val severity = t.gas_severity
                    ?: (if (t.gas_warning == true) "warning" else "clear")
                val severityLabel = when (severity) {
                    "danger" -> "DANGER"
                    "warning" -> "WARNING"
                    "clear" -> "CLEAR"
                    else -> "UNKNOWN"
                }
                val severityColor = when (severity) {
                    "danger" -> Color(0xFFF44336)
                    "warning" -> Color(0xFFFFC107)
                    "clear" -> Color(0xFF4CAF50)
                    else -> Color.Gray
                }
                StatusRowColored("Gas Severity", severityLabel, severityColor)
            } ?: Text("No gas sensor data", color = Color.Gray)
        }

        Spacer(Modifier.height(12.dp))

        // Safety Status
        StatusCard("Safety Status") {
            state.telemetry?.let { t ->
                StatusRow("Safety Stop", if (t.safety_stop == true) "ACTIVE" else "Clear")
                t.safety_alert?.let { StatusRow("Alert", it) }
            } ?: Text("No safety data", color = Color.Gray)
        }

        Spacer(Modifier.height(12.dp))

        // Sensor Timestamp
        state.telemetry?.sensor_ts?.let {
            Text("Last Update: ${formatTime(it * 1000)}", fontSize = 10.sp, color = Color.Gray)
        }
    }
}

@Composable
fun SensorBar(label: String, value: Int?, maxValue: Int) {
    val safeValue = value?.coerceIn(0, maxValue) ?: 0
    val fraction = safeValue.toFloat() / maxValue

    Column(modifier = Modifier.padding(vertical = 4.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text(label, fontSize = 12.sp)
            Text("$safeValue", fontSize = 12.sp, fontWeight = FontWeight.Medium)
        }
        Spacer(Modifier.height(4.dp))
        LinearProgressIndicator(
            progress = { fraction },
            modifier = Modifier
                .fillMaxWidth()
                .height(8.dp)
                .clip(RoundedCornerShape(4.dp)),
            color = when {
                fraction > 0.7f -> Color(0xFFF44336)
                fraction > 0.4f -> Color(0xFFFF9800)
                else -> Color(0xFF4CAF50)
            },
        )
    }
}

// =============================================================================
// LOGS SCREEN - COLLAPSIBLE GROUPS BY SERVICE
// =============================================================================
@Composable
fun LogsScreen(state: AppState, viewModel: AppViewModel) {
    val context = LocalContext.current
    var expandedServices by remember { mutableStateOf(setOf<BackendLogService>()) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        // Header Row
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Logs", style = MaterialTheme.typography.headlineMedium)
            Row {
                IconButton(onClick = { viewModel.refreshBackendLogs() }) {
                    Icon(Icons.Default.Refresh, contentDescription = "Refresh")
                }
                IconButton(onClick = { viewModel.exportLogs(context) }) {
                    Icon(Icons.Default.Download, contentDescription = "Export")
                }
            }
        }

        // Auto-refresh toggle
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Auto-refresh", fontSize = 12.sp)
            Switch(
                checked = state.logAutoRefresh,
                onCheckedChange = { viewModel.setLogAutoRefresh(it) }
            )
        }

        Spacer(Modifier.height(8.dp))

        // Export Result
        state.logExportResult?.let {
            Text(it, fontSize = 10.sp, color = if (it.startsWith("error")) Color.Red else Color.Green)
            Spacer(Modifier.height(8.dp))
        }

        // Collapsible Log Groups
        LazyColumn(modifier = Modifier.fillMaxSize()) {
            // Backend Services
            item {
                Text("Backend Services", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                Spacer(Modifier.height(8.dp))
            }

            BackendLogService.entries.forEach { service ->
                val snapshot = state.backendLogs[service]
                val isExpanded = expandedServices.contains(service)
                val lineCount = snapshot?.lines?.size ?: 0

                item(key = "header_${service.name}") {
                    LogServiceHeader(
                        service = service,
                        lineCount = lineCount,
                        error = snapshot?.error,
                        isExpanded = isExpanded,
                        onClick = {
                            expandedServices = if (isExpanded) {
                                expandedServices - service
                            } else {
                                expandedServices + service
                            }
                        }
                    )
                }

                item(key = "content_${service.name}") {
                    AnimatedVisibility(
                        visible = isExpanded,
                        enter = expandVertically(),
                        exit = shrinkVertically()
                    ) {
                        LogServiceContent(
                            lines = snapshot?.lines.orEmpty(),
                            error = snapshot?.error
                        )
                    }
                }
            }

            // App Logs Section
            item {
                Spacer(Modifier.height(16.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("App Logs (${state.logs.size})", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    TextButton(onClick = { viewModel.clearLogs() }) {
                        Text("Clear", fontSize = 12.sp)
                    }
                }
                Spacer(Modifier.height(8.dp))
            }

            items(state.logs.takeLast(50).reversed(), key = { it.ts }) { entry ->
                AppLogItem(entry)
            }
        }
    }
}

@Composable
fun LogServiceHeader(
    service: BackendLogService,
    lineCount: Int,
    error: String?,
    isExpanded: Boolean,
    onClick: () -> Unit
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp)
            .clickable { onClick() },
        colors = CardDefaults.cardColors(
            containerColor = if (error != null) MaterialTheme.colorScheme.errorContainer
            else MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    if (isExpanded) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                    contentDescription = null,
                    modifier = Modifier.size(20.dp)
                )
                Spacer(Modifier.width(8.dp))
                Text(service.label, fontWeight = FontWeight.Medium)
            }
            Text(
                if (error != null) "Error" else "$lineCount lines",
                fontSize = 12.sp,
                color = if (error != null) MaterialTheme.colorScheme.error else Color.Gray
            )
        }
    }
}

@Composable
fun LogServiceContent(lines: List<String>, error: String?) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 16.dp, end = 0.dp, top = 0.dp, bottom = 8.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(max = 300.dp)
                .padding(8.dp)
        ) {
            if (error != null) {
                Text("Error: $error", color = Color.Red, fontSize = 10.sp)
            } else if (lines.isEmpty()) {
                Text("No logs", color = Color.Gray, fontSize = 10.sp)
            } else {
                LazyColumn {
                    items(lines.takeLast(100).reversed()) { line ->
                        Text(
                            text = line,
                            fontSize = 9.sp,
                            fontFamily = FontFamily.Monospace,
                            modifier = Modifier.padding(vertical = 1.dp)
                        )
                    }
                }
            }
        }
    }
}

@Composable
fun AppLogItem(entry: AppLogEntry) {
    val bgColor = when (entry.category) {
        LogCategory.INTENT -> MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.3f)
        LogCategory.NETWORK -> MaterialTheme.colorScheme.tertiaryContainer.copy(alpha = 0.3f)
        LogCategory.STATE -> MaterialTheme.colorScheme.secondaryContainer.copy(alpha = 0.3f)
        LogCategory.UI -> MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.3f)
    }

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 1.dp),
        colors = CardDefaults.cardColors(containerColor = bgColor)
    ) {
        Text(
            text = entry.toDisplayLine(),
            fontSize = 9.sp,
            fontFamily = FontFamily.Monospace,
            modifier = Modifier.padding(6.dp)
        )
    }
}

// =============================================================================
// SETTINGS SCREEN - IP/PORT CONFIGURATION
// =============================================================================
@Composable
fun SettingsScreen(state: AppState, viewModel: AppViewModel) {
    var editIp by remember(state.settings) { mutableStateOf(state.settings?.robotIp ?: "") }
    var editPort by remember(state.settings) { mutableStateOf(state.settings?.robotPort?.toString() ?: "") }
    var editPollMs by remember(state.settings) { mutableStateOf(state.settings?.pollIntervalMs?.toString() ?: "") }
    var editDebug by remember(state.settings) { mutableStateOf(state.settings?.debugEnabled ?: false) }
    val cameraSettings = state.cameraSettings
    var editGamma by remember(cameraSettings) { mutableStateOf(cameraSettings?.stream_gamma?.toString() ?: "") }
    var editCamWidth by remember(cameraSettings) { mutableStateOf(cameraSettings?.picam2_width?.toString() ?: "") }
    var editCamHeight by remember(cameraSettings) { mutableStateOf(cameraSettings?.picam2_height?.toString() ?: "") }
    var editCamFps by remember(cameraSettings) { mutableStateOf(cameraSettings?.picam2_fps?.toString() ?: "") }
    val editControls = remember(cameraSettings) {
        mutableStateMapOf<String, String>().apply {
            (cameraSettings?.picam2_controls ?: emptyMap()).forEach { (key, value) ->
                put(key, value)
            }
        }
    }
    val defaultGamma = "1.3"
    val defaultCamWidth = "832"
    val defaultCamHeight = "468"
    val defaultCamFps = "12"
    val defaultControls = mapOf(
        "FrameRate" to "12",
        "AwbEnable" to "true",
        "AeEnable" to "true",
        "ColourGains" to "1.0,1.0",
        "ExposureTime" to "0",
        "AnalogueGain" to "1.0",
        "Brightness" to "0.2",
        "Contrast" to "1.0",
        "Saturation" to "1.0",
        "Sharpness" to "1.0",
    )
    var newControlKey by remember { mutableStateOf("") }
    var newControlValue by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(16.dp))

        // Connection Settings
        StatusCard("Connection") {
            OutlinedTextField(
                value = editIp,
                onValueChange = { editIp = it },
                label = { Text("Robot IP") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri)
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = editPort,
                onValueChange = { editPort = it.filter { c -> c.isDigit() } },
                label = { Text("Robot Port") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
            )
        }

        Spacer(Modifier.height(12.dp))

        // Polling Settings
        StatusCard("Polling") {
            OutlinedTextField(
                value = editPollMs,
                onValueChange = { editPollMs = it.filter { c -> c.isDigit() } },
                label = { Text("Poll Interval (ms)") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
            )
        }

        Spacer(Modifier.height(12.dp))

        // Debug Settings
        StatusCard("Debug") {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("Enable Debug Mode")
                Switch(
                    checked = editDebug,
                    onCheckedChange = { editDebug = it }
                )
            }
        }

        Spacer(Modifier.height(16.dp))

        // Camera Settings
        StatusCard("Camera Settings") {
            if (cameraSettings == null) {
                Text("Camera settings not loaded", fontSize = 12.sp, color = Color.Gray)
            }

            OutlinedTextField(
                value = editGamma,
                onValueChange = { editGamma = it },
                label = { Text("Stream Gamma") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
            )
            Spacer(Modifier.height(8.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = editCamWidth,
                    onValueChange = { editCamWidth = it.filter { c -> c.isDigit() } },
                    label = { Text("Picam Width") },
                    modifier = Modifier.weight(1f),
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
                )
                OutlinedTextField(
                    value = editCamHeight,
                    onValueChange = { editCamHeight = it.filter { c -> c.isDigit() } },
                    label = { Text("Picam Height") },
                    modifier = Modifier.weight(1f),
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
                )
            }
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = editCamFps,
                onValueChange = { editCamFps = it.filter { c -> c.isDigit() } },
                label = { Text("Picam FPS") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
            )

            Spacer(Modifier.height(12.dp))
            Text("Picamera2 Controls", fontWeight = FontWeight.Bold)
            val controlKeys = editControls.keys.sorted()
            controlKeys.forEach { key ->
                Spacer(Modifier.height(6.dp))
                OutlinedTextField(
                    value = editControls[key] ?: "",
                    onValueChange = { editControls[key] = it },
                    label = { Text(key) },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true
                )
            }

            Spacer(Modifier.height(8.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = newControlKey,
                    onValueChange = { newControlKey = it },
                    label = { Text("New Control Key") },
                    modifier = Modifier.weight(1f),
                    singleLine = true
                )
                OutlinedTextField(
                    value = newControlValue,
                    onValueChange = { newControlValue = it },
                    label = { Text("New Control Value") },
                    modifier = Modifier.weight(1f),
                    singleLine = true
                )
            }
            Spacer(Modifier.height(6.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = {
                        val key = newControlKey.trim()
                        val value = newControlValue.trim()
                        if (key.isNotEmpty() && value.isNotEmpty()) {
                            editControls[key] = value
                            newControlKey = ""
                            newControlValue = ""
                        }
                    },
                    modifier = Modifier.weight(1f)
                ) {
                    Text("Add Control")
                }
                TextButton(
                    onClick = { viewModel.fetchCameraSettings() },
                    modifier = Modifier.weight(1f)
                ) {
                    Text("Refresh")
                }
            }

            Spacer(Modifier.height(8.dp))
            Button(
                onClick = {
                    val update = com.smartcar.supervision.data.CameraSettingsUpdate(
                        stream_gamma = editGamma.toDoubleOrNull(),
                        picam2_width = editCamWidth.toIntOrNull(),
                        picam2_height = editCamHeight.toIntOrNull(),
                        picam2_fps = editCamFps.toIntOrNull(),
                        picam2_controls = editControls.toMap()
                    )
                    viewModel.updateCameraSettings(update)
                },
                modifier = Modifier.fillMaxWidth()
            ) {
                Icon(Icons.Default.Tune, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Apply Camera Settings")
            }

            state.cameraUpdateStatus?.let { status ->
                Spacer(Modifier.height(6.dp))
                Text("Status: $status", fontSize = 12.sp, color = Color.Gray)
            }
            if (state.cameraUpdateRequiresRestart == true) {
                Spacer(Modifier.height(4.dp))
                Text("Vision restart required for resolution changes", fontSize = 12.sp, color = Color.Red)
            }

            Spacer(Modifier.height(8.dp))
            OutlinedButton(
                onClick = {
                    editGamma = defaultGamma
                    editCamWidth = defaultCamWidth
                    editCamHeight = defaultCamHeight
                    editCamFps = defaultCamFps
                    editControls.clear()
                    editControls.putAll(defaultControls)
                    val update = com.smartcar.supervision.data.CameraSettingsUpdate(
                        stream_gamma = defaultGamma.toDoubleOrNull(),
                        picam2_width = defaultCamWidth.toIntOrNull(),
                        picam2_height = defaultCamHeight.toIntOrNull(),
                        picam2_fps = defaultCamFps.toIntOrNull(),
                        picam2_controls = defaultControls
                    )
                    viewModel.updateCameraSettings(update)
                },
                modifier = Modifier.fillMaxWidth()
            ) {
                Icon(Icons.Default.Refresh, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Reset Camera Defaults")
            }

            Spacer(Modifier.height(8.dp))
            Button(
                onClick = { viewModel.restartVisionService() },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error)
            ) {
                Icon(Icons.Default.Refresh, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("Restart Vision Service")
            }
            state.serviceRestartStatus?.let { status ->
                Spacer(Modifier.height(6.dp))
                Text("Vision restart: $status", fontSize = 12.sp, color = Color.Gray)
            }
        }

        Spacer(Modifier.height(16.dp))

        // Save Button
        Button(
            onClick = {
                val newSettings = AppSettings(
                    robotIp = editIp.ifBlank { "100.111.13.60" },
                    robotPort = editPort.toIntOrNull() ?: 8770,
                    pollIntervalMs = editPollMs.toLongOrNull() ?: 1000L,
                    debugEnabled = editDebug,
                )
                viewModel.updateSettings(newSettings)
            },
            modifier = Modifier.fillMaxWidth()
        ) {
            Icon(Icons.Default.Save, contentDescription = null)
            Spacer(Modifier.width(8.dp))
            Text("Save Settings")
        }

        Spacer(Modifier.height(16.dp))

        // Current Settings Display
        StatusCard("Current Configuration") {
            StatusRow("Base URL", state.settings?.baseUrl() ?: "-")
            StatusRow("Poll Interval", "${state.settings?.pollIntervalMs ?: "-"} ms")
            StatusRow("Debug Mode", if (state.settings?.debugEnabled == true) "Enabled" else "Disabled")
        }

        Spacer(Modifier.height(12.dp))

        // Build Info
        StatusCard("Build Info") {
            StatusRow("Default URL", BuildConfig.ROBOT_BASE_URL)
            StatusRow("Version", "1.0.0")
        }

        Spacer(Modifier.height(12.dp))

        // Diagnostics Section - RESTORED
        var diagExpanded by remember { mutableStateOf(false) }
        StatusCard("Diagnostics") {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { diagExpanded = !diagExpanded },
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("Runtime Diagnostics", fontWeight = FontWeight.Bold)
                Icon(
                    imageVector = if (diagExpanded) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                    contentDescription = if (diagExpanded) "Collapse" else "Expand"
                )
            }
            if (diagExpanded) {
                Spacer(Modifier.height(8.dp))
                Divider()
                Spacer(Modifier.height(8.dp))
                val connLabel = when (state.connection) {
                    is ConnectionStatus.Online -> "Online"
                    is ConnectionStatus.Offline -> "Offline"
                    is ConnectionStatus.Error -> "Error: ${(state.connection as ConnectionStatus.Error).message}"
                }
                StatusRow("Connection Status", connLabel)
                StatusRow("App Status", state.appStatus.name)
                StatusRow("Intent In Flight", if (state.intentInFlight) "Yes" else "No")
                state.lastIntentSent?.let { StatusRow("Last Intent", it) }
                state.lastIntentResult?.let { StatusRow("Last Result", it) }
                state.lastIntentAt?.let { StatusRow("Last Intent At", formatTime(it)) }
                state.blockingReason?.let {
                    Spacer(Modifier.height(4.dp))
                    Text("Blocking Reason:", fontWeight = FontWeight.Bold, color = Color.Red)
                    Text(it, fontSize = 12.sp, color = Color.Red)
                }
            }
        }

        Spacer(Modifier.height(12.dp))

        // Known Limitations Section - RESTORED
        var limExpanded by remember { mutableStateOf(false) }
        StatusCard("Known Limitations") {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { limExpanded = !limExpanded },
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("Platform Notes", fontWeight = FontWeight.Bold)
                Icon(
                    imageVector = if (limExpanded) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                    contentDescription = if (limExpanded) "Collapse" else "Expand"
                )
            }
            if (limExpanded) {
                Spacer(Modifier.height(8.dp))
                Divider()
                Spacer(Modifier.height(8.dp))
                val limitations = listOf(
                    "• MJPEG stream may have latency over cellular",
                    "• Vision intents require backend vision module active",
                    "• Assistant intents require LLM endpoint configured",
                    "• Control intents are queued; only one executes at a time",
                    "• Collision avoidance may override direction commands",
                    "• Wake-word detection requires continuous audio stream",
                    "• TTS playback blocks other audio operations"
                )
                limitations.forEach { lim ->
                    Text(lim, fontSize = 12.sp, color = Color.Gray)
                    Spacer(Modifier.height(4.dp))
                }
            }
        }
    }
}

// =============================================================================
// UTILITY FUNCTIONS
// =============================================================================
fun formatTime(epochMs: Long): String {
    val instant = java.time.Instant.ofEpochMilli(epochMs)
    val zoned = instant.atZone(java.time.ZoneId.systemDefault())
    return java.time.format.DateTimeFormatter.ofPattern("HH:mm:ss").format(zoned)
}
