tell application "System Events"
    set appList to name of every application process whose visible is true
end tell
repeat with appName in appList
    try
        tell application appName
            repeat with w in every window
                try
                    if miniaturized of w is true then
                        set miniaturized of w to false
                    end if
                end try
            end repeat
        end tell
    end try
end repeat
