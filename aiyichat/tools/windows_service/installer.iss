; 爱易聊 AIYIchat Windows 安装包（Inno Setup 6）
; 由 build_installer.py 调用 ISCC 编译，可传入预处理定义：
;   /DMyAppVersion=1.0.0 /DMyBranch=developing /DMyBuildDate=YYYYMMDD /DAppSourceDir=... /DMongoDir=... /DOutputDir=...
;
; 安装内容：GUI 服务面板（含 FastAPI Web 服务）+ 捆绑 MongoDB + ffmpeg + 证书
; 安装动作：注册 mongod 为 Windows 服务并启动、可选开机自启服务面板、首次生成 .env、创建快捷方式

#define MyAppName "爱易聊 AIYIchat"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef MyBranch
  #define MyBranch "developing"
#endif
#ifndef AppSourceDir
  #define AppSourceDir "dist\AIYIchatService"
#endif
#ifndef MongoDir
  #define MongoDir "vendor\mongodb"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef MyBuildDate
  #define MyBuildDate ""
#endif
#if MyBuildDate == ""
  #define OutputSuffix ""
#else
  #define OutputSuffix "_" + MyBuildDate
#endif

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=AIYI
DefaultDirName={autopf}\AIYIchat
DefaultGroupName={#MyAppName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
OutputDir={#OutputDir}
OutputBaseFilename=AIYIchat_Setup_{#MyBranch}{#OutputSuffix}
SetupIconFile=assets\app_icon.ico
Compression=lzma2/max
SolidCompression=yes
UninstallDisplayIcon={app}\AIYIchatService\AIYIchatService.exe
DisableProgramGroupPage=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
; 数据与日志目录需先于 mongod 服务注册存在
Name: "{app}\AIYIchatService\mongo_data"
Name: "{app}\AIYIchatService\logs"

[Tasks]
; 开机自启服务面板（面板自带单实例互斥保护，重复拉起不会冲突）
Name: "autostart"; Description: "开机自动启动爱易聊服务"; GroupDescription: "开机启动:"; Flags: checkedonce

[Files]
; GUI 服务面板与应用主体（PyInstaller onedir 产物）
Source: "{#AppSourceDir}\*"; DestDir: "{app}\AIYIchatService"; Flags: recursesubdirs ignoreversion
; 捆绑 MongoDB（免安装版，仅 mongod）
Source: "{#MongoDir}\bin\*"; DestDir: "{app}\mongodb\bin"; Flags: ignoreversion
; 首次安装生成 .env（已存在则保留用户配置）
Source: "{#AppSourceDir}\.env.default"; DestDir: "{app}\AIYIchatService"; DestName: ".env"; Flags: onlyifdoesntexist

[Icons]
Name: "{autodesktop}\爱易聊服务面板"; Filename: "{app}\AIYIchatService\AIYIchatService.exe"
Name: "{group}\爱易聊服务面板"; Filename: "{app}\AIYIchatService\AIYIchatService.exe"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; 开机自启注册表项（HKLM Run 键覆盖所有用户；绑定 autostart 任务，卸载时由 uninsdeletevalue 自动清理）
Root: HKLM; Subkey: "SOFTWARE\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "AIYIchatService"; ValueData: """{app}\AIYIchatService\AIYIchatService.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; 安装完成后可选立即启动服务面板（mongod 服务注册在 [Code] 中以提权身份完成）
Filename: "{app}\AIYIchatService\AIYIchatService.exe"; Description: "立即启动爱易聊服务面板"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 停止并移除 mongod 服务（卸载器本身已提权；7.x 需 --serviceName 指定服务名）
Filename: "net"; Parameters: "stop AIYIchatMongoDB"; Flags: runhidden waituntilterminated; RunOnceId: "StopMongoDB"
Filename: "{app}\mongodb\bin\mongod.exe"; Parameters: "--remove --serviceName AIYIchatMongoDB"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveMongoDB"

[Code]
var
  MongoInstallFailed: Boolean;

// 安装后注册并启动 mongod 服务（安装器已提权，直接执行无需再次授权）
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Params: String;
begin
  if CurStep = ssPostInstall then
  begin
    MongoInstallFailed := False;
    // 覆盖安装场景：先移除同名残留服务（指向旧路径），避免 1078 冲突
    Exec(ExpandConstant('{app}\mongodb\bin\mongod.exe'),
         '--remove --serviceName AIYIchatMongoDB', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Params := '--install --serviceName AIYIchatMongoDB --service' +
      ' --serviceDisplayName AIYIchatMongoDB' +
      ' --dbpath "' + ExpandConstant('{app}\AIYIchatService\mongo_data') + '"' +
      ' --logpath "' + ExpandConstant('{app}\AIYIchatService\logs\mongod.log') + '"' +
      ' --logappend --bind_ip 127.0.0.1 --port 27017';
    if not Exec(ExpandConstant('{app}\mongodb\bin\mongod.exe'), Params, '',
                 SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    begin
      MongoInstallFailed := True;
      SuppressibleMsgBox('MongoDB 服务注册失败（错误码 ' + IntToStr(ResultCode) + '），' +
        '请稍后以管理员身份手动执行安装目录下的 mongodb\bin\mongod.exe 注册命令。',
        mbError, MB_OK, IDOK);
    end
    else if not Exec('net', 'start AIYIchatMongoDB', '', SW_HIDE,
                     ewWaitUntilTerminated, ResultCode) then
    begin
      MongoInstallFailed := True;
    end;
  end;
end;

// 卸载后询问是否保留数据库数据
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('是否同时删除数据库数据（聊天记录、角色等业务数据）？' #13#10 +
              '选择"否"将保留数据，重新安装后可继续使用。',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      DelTree(ExpandConstant('{app}\AIYIchatService\mongo_data'), True, True, True);
      DelTree(ExpandConstant('{app}\AIYIchatService\logs'), True, True, True);
    end;
  end;
end;
