# 【新增 v2.11】本机拖放面板：把文件 / 整个文件夹拖进来，识别完整路径后回写网页。
# 由 server.py 以独立进程调起（powershell -NoProfile -STA -File），
# 结果用一行 JSON 数组输出到 stdout；网页拿到路径后走 /api/link/batch 硬链接导入。
# 浏览器拿不到拖入文件的本地路径，这一层就是补上这个缺口。
param()

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$script:result = @()
$script:seen = @{}
$exts = @('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm',
          '.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.wma')

$font = New-Object System.Drawing.Font('Microsoft YaHei', 9)
$form = New-Object System.Windows.Forms.Form
$form.Text = '批量导入本机课件'
$form.Size = New-Object System.Drawing.Size(780, 470)
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.AllowDrop = $true
$form.MinimizeBox = $false
$form.MaximizeBox = $false
$form.FormBorderStyle = 'FixedDialog'
$form.Font = $font

$tips = New-Object System.Windows.Forms.Label
$tips.Location = New-Object System.Drawing.Point(12, 10)
$tips.Size = New-Object System.Drawing.Size(740, 40)
$tips.Text = '把课件文件（或整个文件夹）从资源管理器拖进这个窗口；也可以点「浏览文件…」一次多选。'
$form.Controls.Add($tips)

$list = New-Object System.Windows.Forms.ListBox
$list.Location = New-Object System.Drawing.Point(12, 54)
$list.Size = New-Object System.Drawing.Size(740, 310)
$list.SelectionMode = 'MultiExtended'
$list.HorizontalScrollbar = $true
$form.Controls.Add($list)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Text = '浏览文件…'
$btnBrowse.Location = New-Object System.Drawing.Point(12, 376)
$btnBrowse.Size = New-Object System.Drawing.Size(110, 34)
$form.Controls.Add($btnBrowse)

$btnRemove = New-Object System.Windows.Forms.Button
$btnRemove.Text = '移除选中'
$btnRemove.Location = New-Object System.Drawing.Point(130, 376)
$btnRemove.Size = New-Object System.Drawing.Size(100, 34)
$form.Controls.Add($btnRemove)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = '取消'
$btnCancel.Location = New-Object System.Drawing.Point(556, 376)
$btnCancel.Size = New-Object System.Drawing.Size(90, 34)
$form.Controls.Add($btnCancel)

$btnOk = New-Object System.Windows.Forms.Button
$btnOk.Text = '导入到网页'
$btnOk.Location = New-Object System.Drawing.Point(654, 376)
$btnOk.Size = New-Object System.Drawing.Size(98, 34)
$form.Controls.Add($btnOk)

function Update-Tip {
    $tips.Text = "已加入 $($list.Items.Count) 个文件。可继续拖入，或点「导入到网页」写回网页批量零拷贝导入。"
}

function Add-Path {
    param([string]$p)
    if ([string]::IsNullOrWhiteSpace($p)) { return }
    if ([System.IO.Directory]::Exists($p)) {
        # 拖文件夹：递归展开里面的音视频（用户课件常按目录存放）
        try {
            $files = [System.IO.Directory]::GetFiles($p, '*', [System.IO.SearchOption]::AllDirectories)
            foreach ($f in $files) { Add-Path $f }
        } catch { }
        return
    }
    if (-not [System.IO.File]::Exists($p)) { return }
    $ext = [System.IO.Path]::GetExtension($p).ToLower()
    if ($exts -notcontains $ext) { return }
    $full = [System.IO.Path]::GetFullPath($p)
    if (-not $script:seen.ContainsKey($full)) {
        $script:seen[$full] = $true
        [void]$list.Items.Add($full)
    }
}

$form.Add_DragEnter({
    param($sender, $e)
    if ($e.Data.GetDataPresent([System.Windows.Forms.DataFormats]::FileDrop)) {
        $e.Effect = [System.Windows.Forms.DragDropEffects]::Copy
    } else {
        $e.Effect = [System.Windows.Forms.DragDropEffects]::None
    }
})

$form.Add_DragDrop({
    param($sender, $e)
    $items = $e.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop)
    foreach ($p in $items) { Add-Path $p }
    Update-Tip
})

$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Multiselect = $true
    $dlg.Title = '选择本机课件（可多选，也可把文件拖进本对话框）'
    $dlg.Filter = ('音视频课件|*.mp4;*.mkv;*.avi;*.mov;*.flv;*.wmv;*.webm;' +
                   '*.mp3;*.m4a;*.wav;*.flac;*.aac;*.ogg;*.wma|所有文件|*.*')
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        foreach ($f in $dlg.FileNames) { Add-Path $f }
    }
    $dlg.Dispose()
    Update-Tip
})

$btnRemove.Add_Click({
    $sel = @($list.SelectedItems)
    foreach ($x in $sel) {
        $script:seen.Remove($x)
        $list.Items.Remove($x)
    }
    Update-Tip
})

$btnOk.Add_Click({
    $script:result = @()
    foreach ($x in $list.Items) { $script:result += $x }
    $form.Close()
})

$btnCancel.Add_Click({
    $script:result = @()
    $form.Close()
})

# 5 分钟没操作自动关，避免服务端请求一直挂着
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 300000
$timer.Add_Tick({ $form.Close() })
$timer.Start()
$form.Add_Shown({ $form.Activate(); $form.BringToFront() })

[void]$form.ShowDialog()
$timer.Stop()
$form.Dispose()

if ($script:result.Count -eq 0) {
    '[]'
} else {
    ConvertTo-Json -InputObject @($script:result) -Compress
}
