# 获取参数（预期是一个 JSON 文件路径）
param(
    [string]$jsonFilePath
)

# 初始化关键词变量
$keyword = ""

# 如果提供了参数且文件存在
if (-not [string]::IsNullOrEmpty($jsonFilePath) -and (Test-Path $jsonFilePath)) {
    try {
        # 读取 JSON 文件内容
        $jsonContent = Get-Content -Path $jsonFilePath -Raw -Encoding UTF8 -ErrorAction Stop
        
        # 检查文件内容是否为空
        if (-not [string]::IsNullOrWhiteSpace($jsonContent)) {
            # 解析 JSON
            $jsonObject = $jsonContent | ConvertFrom-Json -ErrorAction Stop
            
            # 根据实际的 JSON 结构获取翻译文本
            # 路径: data.translate_text
            if ($null -ne $jsonObject.data -and $null -ne $jsonObject.data.translate_text) {
                $translateText = $jsonObject.data.translate_text
                
                # 判断是数组还是字符串
                if ($translateText -is [array]) {
                    # 如果是数组，用空格连接所有元素
                    $keyword = $translateText -join " "
                    Write-Host "从 JSON 文件的 data.translate_text 数组读取到内容: '$keyword'" -ForegroundColor Cyan
                }
                else {
                    # 如果是字符串，直接使用
                    $keyword = $translateText.ToString().Trim()
                    Write-Host "从 JSON 文件的 data.translate_text 字段读取到内容: '$keyword'" -ForegroundColor Cyan
                }
            }
            else {
                Write-Host "JSON 文件中未找到 'data.translate_text' 字段" -ForegroundColor Yellow
                Write-Host "实际的 JSON 结构: $($jsonObject | ConvertTo-Json -Compress)" -ForegroundColor Gray
            }
        }
        else {
            Write-Host "JSON 文件内容为空" -ForegroundColor Yellow
        }
        
    }
    catch {
        Write-Host "读取或解析 JSON 文件时出错: $_" -ForegroundColor Red
        $keyword = ""
    }
}
else {
    Write-Host "未提供有效 JSON 文件路径或文件不存在: $jsonFilePath" -ForegroundColor Yellow
}

# 根据是否有关键词构建 URL
if ([string]::IsNullOrWhiteSpace($keyword)) {
    $url = "https://fanyi.sogou.com"
    Write-Host "无翻译内容，直接打开搜狗翻译首页" -ForegroundColor Yellow
}
else {
    # 对关键词进行 URL 编码（处理特殊字符和中文）
    Add-Type -AssemblyName System.Web
    $encodedKeyword = [System.Web.HttpUtility]::UrlEncode($keyword)
    
    # 构建带参数的 URL
    $url = "https://fanyi.sogou.com/text?keyword=$encodedKeyword"
    
    # 显示生成的 URL（可选）
    Write-Host "生成的翻译 URL（已编码）" -ForegroundColor Green
    Write-Host "原始内容: $keyword" -ForegroundColor Gray
}

# 用默认浏览器打开 URL
try {
    Write-Host "正在打开浏览器..." -ForegroundColor Cyan
    Start-Process $url
    if ([string]::IsNullOrWhiteSpace($keyword)) {
        Write-Host "已在默认浏览器中打开搜狗翻译首页" -ForegroundColor Green
    }
    else {
        Write-Host "已在默认浏览器中打开搜狗翻译页面" -ForegroundColor Green
    }
}
catch {
    Write-Host "打开浏览器时出错: $_" -ForegroundColor Red
}
