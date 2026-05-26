# Drifox 会话查询 PowerShell 脚本
# 用法: .\query_sessions.ps1 -Date "2026-05-07"
# 默认查询昨天
#
# 路径说明:
#   脚本位置: skills/session-summary/scripts/query_sessions.ps1
#   数据库位置: .drifox/sessions.db (相对于 Drifox 安装目录)

param(
    [string]$Date = $null,
    [string]$SessionId = $null,
    [switch]$ListOnly
)

# 基于脚本位置计算 Drifox 根目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir = Split-Path -Parent $ScriptDir
$SkillsDir = Split-Path -Parent $SkillDir
$InternalDir = Split-Path -Parent $SkillsDir
$DrifoxRoot = Split-Path -Parent $InternalDir
$DB_PATH = Join-Path $DrifoxRoot ".drifox\sessions.db"

function Query-Sessions {
    param([string]$DateStr)

    if (-not (Test-Path $DB_PATH)) {
        Write-Output "Error: Database not found at $DB_PATH"
        Write-Output "Drifox root: $DrifoxRoot"
        return @()
    }

    $results = @()
    $conn = New-Object System.Data.SQLite.SQLiteConnection
    $conn.ConnectionString = "Data Source=$DB_PATH;Version=3;"
    $conn.Open()

    $cmd = $conn.CreateCommand()
    $cmd.CommandText = "SELECT session_id, title, message_count, project, created_at, messages FROM sessions WHERE created_at LIKE '$DateStr%' ORDER BY created_at DESC"

    $reader = $cmd.ExecuteReader()
    while ($reader.Read()) {
        $results += @{
            session_id = $reader["session_id"]
            title = $reader["title"]
            message_count = $reader["message_count"]
            project = $reader["project"]
            created_at = $reader["created_at"]
            messages = $reader["messages"]
        }
    }

    $reader.Close()
    $conn.Close()

    return $results
}

function Format-Session {
    param($Session)

    $title = if ($Session.title) { $Session.title } else { "(无标题)" }

    Write-Output ""
    Write-Output ("=" * 60)
    Write-Output "标题: $title"
    Write-Output "项目: $($Session.project)"
    Write-Output "消息数: $($Session.message_count)"
    Write-Output "创建时间: $($Session.created_at)"
    Write-Output ("=" * 60)

    if ($Session.messages -and -not $ListOnly) {
        try {
            $msgs = $Session.messages | ConvertFrom-Json
            Write-Output ""
            Write-Output "消息内容摘要 (共 $($msgs.Count) 条):"
            $count = 0
            foreach ($msg in $msgs) {
                $count++
                if ($count -gt 10) { break }
                $role = $msg.role
                $content = $msg.content
                if ($content.Length -gt 150) {
                    $content = $content.Substring(0, 150) + "..."
                }
                Write-Output "  [$count] [$role]: $content"
            }
            if ($msgs.Count -gt 10) {
                Write-Output "  ... 还有 $($msgs.Count - 10) 条消息"
            }
        } catch {
            Write-Output "消息内容 (解析失败): $($Session.messages.Substring(0, [Math]::Min(500, $Session.messages.Length)))..."
        }
    }
    Write-Output ""
}

# 确定查询日期
if (-not $Date) {
    $Date = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd")
}

Write-Output ""
Write-Output ("#" * 60)
Write-Output "# Drifox 会话记录 - $Date"
Write-Output "# 数据库: $DB_PATH"
Write-Output ("#" * 60)
Write-Output ""

if ($SessionId) {
    $conn = New-Object System.Data.SQLite.SQLiteConnection
    $conn.ConnectionString = "Data Source=$DB_PATH;Version=3;"
    $conn.Open()

    $cmd = $conn.CreateCommand()
    $cmd.CommandText = "SELECT session_id, title, message_count, project, created_at, messages FROM sessions WHERE session_id = '$SessionId'"

    $reader = $cmd.ExecuteReader()
    if ($reader.Read()) {
        $session = @{
            session_id = $reader["session_id"]
            title = $reader["title"]
            message_count = $reader["message_count"]
            project = $reader["project"]
            created_at = $reader["created_at"]
            messages = $reader["messages"]
        }
        Format-Session $session
    } else {
        Write-Output "未找到会话: $SessionId"
    }

    $reader.Close()
    $conn.Close()
} else {
    $sessions = Query-Sessions $Date
    Write-Output "# 共找到 $($sessions.Count) 条会话"
    Write-Output "# 查询时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    Write-Output ""

    $i = 1
    foreach ($session in $sessions) {
        Write-Output "[$i] $(if ($session.title) { $session.title } else { '(无标题)' }) | 项目:$($session.project) | 消息:$($session.message_count) | $($session.created_at.Substring(0, 16))"
        if (-not $ListOnly) {
            Format-Session $session
        }
        $i++
    }
}
