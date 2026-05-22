-- MyCalFix: URL scheme handler for mycalfix://fix?...
--
-- Compiled into ~/Applications/MyCalFix.app by scripts/install_app.sh.
-- install_app.sh also copies launch_fix.sh + fix_prompt.md into
-- Contents/Resources/, so the .app is self-contained and never reads files
-- from the user's repo location (which may sit under a TCC-protected folder
-- like ~/Desktop, ~/Documents, or ~/Downloads — those would EPERM the .app).

on open location this_URL
	try
		set bundle_path to POSIX path of (path to me)
		set launcher to bundle_path & "Contents/Resources/launch_fix.sh"
		do shell script quoted form of launcher & " " & quoted form of this_URL
	on error errMsg number errNum
		display alert "MyCalFix 启动失败" message "URL: " & this_URL & return & return & "错误：" & errMsg & " (" & errNum & ")"
	end try
end open location

on run
	display dialog "MyCalFix 是一个 URL handler，需要由 mycalfix:// 链接触发（例如日历事件里的链接）。" buttons {"OK"} default button 1 with icon note
end run
