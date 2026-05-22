-- MyCalFix: URL scheme handler for mycalfix://fix?...
--
-- Compiled into ~/Applications/MyCalFix.app by scripts/install_app.sh.
-- The placeholder __LAUNCHER_PATH__ is replaced with the absolute path of
-- scripts/launch_fix.sh at install time, so the .app remains independent of
-- the repo's location.

on open location this_URL
	try
		do shell script "__LAUNCHER_PATH__ " & quoted form of this_URL
	on error errMsg number errNum
		display alert "MyCalFix 启动失败" message "URL: " & this_URL & return & return & "错误：" & errMsg & " (" & errNum & ")"
	end try
end open location

on run
	display dialog "MyCalFix 是一个 URL handler，需要由 mycalfix:// 链接触发（例如日历事件里的链接）。" buttons {"OK"} default button 1 with icon note
end run
