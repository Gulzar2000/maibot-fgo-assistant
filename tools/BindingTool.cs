using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace FgoBinding {
    internal static class Program {
        internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 1048576 };
        internal const string ErrorLogName = "FGO绑定工具-错误.log";
        static readonly object ErrorLogLock = new object();

        internal static bool RecordError(string mode, string phase, string code, string detail = null) {
            string line = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz") + " [" + mode + "/" + phase + "] " + code;
            if (!String.IsNullOrEmpty(detail)) line += " | " + Regex.Replace(detail, "[\\r\\n\\t]+", " ");
            string path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ErrorLogName);
            try {
                lock (ErrorLogLock) File.AppendAllText(path, line + Environment.NewLine, new System.Text.UTF8Encoding(false));
                return true;
            } catch (IOException) {
                return false;
            } catch (UnauthorizedAccessException) {
                return false;
            }
        }
        [STAThread] static int Main(string[] args) {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            try {
                string root = Prepare(args.Length == 2 && args[0] == "--self-test" ? Path.Combine(Path.GetFullPath(args[1]), "runtime") : null);
                AppDomain.CurrentDomain.AssemblyResolve += (sender, e) => {
                    string library = new AssemblyName(e.Name).Name;
                    if (library != "Microsoft.Web.WebView2.Core" && library != "Microsoft.Web.WebView2.WinForms") return null;
                    // 只解析 EXE 内置的两个依赖，不从任意目录或网络位置加载程序集。
                    using (var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
                    using (var zip = new ZipArchive(resource, ZipArchiveMode.Read))
                    using (var input = zip.GetEntry("webview2/" + library + ".dll").Open())
                    using (var buffer = new MemoryStream()) {
                        input.CopyTo(buffer);
                        return Assembly.Load(buffer.ToArray());
                    }
                };
                if (args.Length >= 2 && args[0] == "--check-webview") {
                    string report = Path.GetFullPath(args[1]);
                    using (var form = new BindingForm(root)) {
                        form.ShowInTaskbar = false;
                        form.StartPosition = FormStartPosition.Manual;
                        form.Location = new Point(-32000, -32000);
                        string testAddress = args.Length == 3 ? args[2] : "about:blank";
                        form.Shown += async (sender, e) => await form.CheckVerificationBrowser(report, testAddress);
                        Application.Run(form);
                    }
                    return File.Exists(report) && File.ReadAllText(report) == "navigation-ok" ? 0 : 1;
                }
                if (args.Length == 2 && args[0] == "--verify") {
                    string directory = Path.GetFullPath(args[1]); Directory.CreateDirectory(directory);
                    using (var form = new BindingForm(root)) {
                        form.StartPosition = FormStartPosition.Manual; form.Location = new Point(-32000, -32000);
                        form.ShowInTaskbar = false; form.Show(); Application.DoEvents();
                        using (var bitmap = new Bitmap(form.Width, form.Height)) {
                            form.DrawToBitmap(bitmap, new Rectangle(0, 0, bitmap.Width, bitmap.Height));
                            bitmap.Save(Path.Combine(directory, "tool-preview.png"));
                        }
                        form.Hide();
                    }
                    using (var process = Start(root, "12345678", "android", Path.Combine(directory, "12345678.fgobind"), true)) {
                        string output = process.StandardOutput.ReadToEnd(); string error = process.StandardError.ReadToEnd(); process.WaitForExit();
                        File.WriteAllText(Path.Combine(directory, "verify-report.txt"), "Runtime: " + root + "\r\nGUI: OK\r\nBackend exit: " + process.ExitCode + "\r\n" + output + error);
                        return process.ExitCode;
                    }
                }
                if (args.Length == 2 && args[0] == "--check-startup") {
                    File.WriteAllText(args[1], "OK\r\nRuntime: " + root); return 0;
                }
                if (args.Length == 2 && args[0] == "--self-test") {
                    string directory = Path.GetFullPath(args[1]); Directory.CreateDirectory(directory);
                    using (var process = Start(root, "12345678", "android", Path.Combine(directory, "12345678.fgobind"), true)) {
                        process.StandardOutput.ReadToEnd(); process.StandardError.ReadToEnd(); process.WaitForExit(); return process.ExitCode;
                    }
                }
                Application.Run(new BindingForm(root)); return 0;
            } catch (Exception ex) {
                if (args.Length == 2 && args[0] == "--verify") {
                    File.WriteAllText(Path.Combine(args[1], "verify-report.txt"), ex.ToString()); return 1;
                }
                if (args.Length == 2 && args[0] == "--check-startup") {
                    File.WriteAllText(args[1], ex.ToString()); return 1;
                }
                if (args.Length >= 2 && args[0] == "--check-webview") {
                    File.WriteAllText(args[1], ex.ToString()); return 1;
                }
                if (args.Length == 2 && args[0] == "--self-test") {
                    File.WriteAllText(Path.Combine(args[1], "launcher-error.txt"), ex.ToString()); return 1;
                }
                RecordError("launcher", "startup", ex.GetType().Name, "HRESULT=" + ex.HResult.ToString("X8"));
                MessageBox.Show("无法准备本地运行文件：\r\n" + ex.Message + "\r\n\r\n错误类型：" + ex.GetType().Name,
                                "FGO账号绑定工具", MessageBoxButtons.OK, MessageBoxIcon.Error); return 1;
            }
        }
        static string Hash(Stream stream) {
            using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }
        static string Prepare(string testRoot) {
            var asm = Assembly.GetExecutingAssembly(); string hash;
            using (var stream = asm.GetManifestResourceStream("payload.zip")) hash = Hash(stream);
            // 尽量解包到 EXE 同目录。深目录使用系统临时目录，以便 WebView2 也有空间创建缓存路径。
            string version = hash.Substring(0, 20);
            string root = testRoot ?? Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".fgo-runtime", version);
            if (root.Length > 150) root = Path.Combine(Path.GetTempPath(), "FgoBinding-" + version);
            Directory.CreateDirectory(root);
            // 每次校验内置资源，避免复用被修改过的协议或加密公钥。
            using (var stream = asm.GetManifestResourceStream("payload.zip")) using (var zip = new ZipArchive(stream, ZipArchiveMode.Read)) {
                foreach (var entry in zip.Entries) {
                    if (String.IsNullOrEmpty(entry.Name)) continue;
                    string path = Path.GetFullPath(Path.Combine(root, entry.FullName));
                    if (!path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException();
                    bool matches = false;
                    using (var input = entry.Open()) { string expected = Hash(input); if (File.Exists(path)) using (var existing = File.OpenRead(path)) matches = Hash(existing) == expected; }
                    if (!matches) {
                        Directory.CreateDirectory(Path.GetDirectoryName(path));
                        using (var input = entry.Open()) using (var output = File.Create(path)) input.CopyTo(output);
                    }
                }
            }
            return root;
        }
        internal static Process Start(string root, string qq, string server, string output, bool selfTest, bool passwordLogin = false) {
            var info = new ProcessStartInfo(Path.Combine(root, "python", "python.exe"));
            info.Arguments = "-I -B -u \"" + Path.Combine(root, "backend", "local_login.py") + "\" --qq " + qq + " --server " + server + " --output \"" + output + "\"" + (selfTest ? " --self-test" : "");
            if (passwordLogin) info.Arguments += " --password-login";
            info.UseShellExecute = false; info.CreateNoWindow = true;
            info.RedirectStandardInput = passwordLogin;
            info.RedirectStandardOutput = true; info.RedirectStandardError = true;
            info.StandardOutputEncoding = System.Text.Encoding.UTF8;
            info.StandardErrorEncoding = System.Text.Encoding.UTF8;
            return Process.Start(info);
        }
        internal static async Task SendBackendInput(Stream input, byte[] line) {
            await input.WriteAsync(line, 0, line.Length);
            await input.FlushAsync();
        }
    }
    internal sealed class BindingForm : Form {
        readonly string root;
        readonly TextBox qq = new TextBox { Left = 120, Top = 25, Width = 260 };
        readonly ComboBox server = new ComboBox { Left = 120, Top = 65, Width = 260, DropDownStyle = ComboBoxStyle.DropDownList };
        readonly Button start = new Button { Left = 35, Top = 110, Width = 170, Height = 35, Text = "扫码绑定" };
        readonly Button passwordStart = new Button { Left = 210, Top = 110, Width = 170, Height = 35, Text = "账密绑定" };
        readonly PictureBox qr = new PictureBox { Left = 55, Top = 155, Width = 310, Height = 310, SizeMode = PictureBoxSizeMode.Zoom };
        readonly Button verify = new Button { Left = 100, Top = 220, Width = 215, Height = 35, Text = "打开安全验证页", Visible = false };
        readonly Label status = new Label { Left = 25, Top = 475, Width = 375, Height = 85, Text = "填写要绑定的 QQ 号，选择游戏账号所在区服。\n生成文件后，仅私发给 FGO小助手管理员。" };
        Process process;
        Uri verificationPage;
        Form verificationWindow;
        Task<CoreWebView2Environment> verificationEnvironment;
        bool webviewLoaderConfigured;
        bool busy;
        bool cancelRequested;
        internal BindingForm(string directory) {
            root = directory; Text = "FGO小助手 · 本地绑定（便携版）"; ClientSize = new Size(415, 565);
            FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
            Controls.Add(new Label { Left = 30, Top = 28, Text = "绑定 QQ 号", Width = 90 });
            Controls.Add(new Label { Left = 30, Top = 68, Text = "FGO 区服", Width = 90 });
            server.Items.AddRange(new object[] { "哔哩安卓", "iOS", "享游安卓" }); server.SelectedIndex = 0;
            Controls.AddRange(new Control[] { qq, server, start, passwordStart, qr, verify, status });
            verify.BringToFront();
            server.SelectedIndexChanged += (sender, e) => passwordStart.Enabled = !busy && server.SelectedIndex != 2;
            start.Click += async (sender, e) => await Run(false);
            passwordStart.Click += async (sender, e) => await Run(true);
            verify.Click += async (sender, e) => await OpenVerificationPage();
            FormClosing += (sender, e) => {
                if (verificationWindow != null) verificationWindow.Close();
                if (process != null && !process.HasExited) process.Kill();
            };
        }
        internal async Task CheckVerificationBrowser(string report, string address) {
            verificationPage = address == "about:blank" ? new Uri(address) : ParseVerificationPage(address, true);
            int completed = 0;
            Action<string> onNavigation = null;
            onNavigation = result => BeginInvoke(new Action(async () => {
                if (result == "navigation-ok" && ++completed == 1) {
                    if (verificationWindow != null) verificationWindow.Close();
                    await OpenVerificationPage(onNavigation);
                } else {
                    File.WriteAllText(report, result);
                    Close();
                }
            }));
            await OpenVerificationPage(onNavigation);
        }
        static Uri ParseVerificationPage(string value, bool fixture = false) {
            Uri address;
            if (!Uri.TryCreate(value, UriKind.Absolute, out address)
                || !String.IsNullOrEmpty(address.UserInfo + address.Fragment))
                throw new InvalidDataException("Invalid verification page");
            if (fixture && address.Scheme == "http" && address.Host == "127.0.0.1"
                && address.Query.Length == 0 && Regex.IsMatch(address.AbsolutePath, "^/verify/[A-Za-z0-9_-]{32}$"))
                return address;
            if (address.Scheme != "https" || address.Host != "game.bilibili.com" || address.Port != 443
                || address.AbsolutePath != "/sdk/geetest_v2/"
                || !Regex.IsMatch(address.Query, @"^\?code=0&captcha_type=1&gt=[A-Za-z0-9_-]{16,128}&challenge=[A-Za-z0-9_-]{16,128}$"))
                throw new InvalidDataException("Invalid verification page");
            return address;
        }
        async Task OpenVerificationPage(Action<string> onNavigation = null) {
            if (verificationPage == null) return;
            if (verificationWindow != null) { verificationWindow.Activate(); return; }
            var address = verificationPage;
            var available = Screen.FromControl(this).WorkingArea;
            var window = new Form {
                Text = "FGO 账号绑定 · 安全验证",
                ClientSize = new Size(Math.Min(430, available.Width - 40), Math.Min(830, available.Height - 80)),
                StartPosition = FormStartPosition.CenterParent, MinimizeBox = false
            };
            var webview = new WebView2 { Dock = DockStyle.Fill };
            var message = new Label { Dock = DockStyle.Top, Height = 32, Text = "正在加载安全验证……" };
            window.Controls.Add(webview);
            window.Controls.Add(message);
            verificationWindow = window;
            window.FormClosed += (sender, e) => { verificationWindow = null; window.Dispose(); };
            window.Show(this);
            string step = "初始化 WebView2";
            try {
                if (!webviewLoaderConfigured) {
                    CoreWebView2Environment.SetLoaderDllFolderPath(Path.Combine(root, "webview2"));
                    webviewLoaderConfigured = true;
                }
                if (verificationEnvironment == null || verificationEnvironment.IsFaulted || verificationEnvironment.IsCanceled)
                    verificationEnvironment = CoreWebView2Environment.CreateAsync(null, Path.Combine(root, "wvdata"));
                var environment = await verificationEnvironment;
                if (window.IsDisposed) return;
                await webview.EnsureCoreWebView2Async(environment);
                if (window.IsDisposed) return;
                webview.CoreWebView2.Settings.AreDevToolsEnabled = false;
                EventHandler adjustZoom = (sender, e) => {
                    webview.ZoomFactor = Math.Max(0.60, Math.Min(1.0, webview.ClientSize.Height / 760.0));
                };
                webview.ClientSizeChanged += adjustZoom;
                adjustZoom(webview, EventArgs.Empty);
                webview.CoreWebView2.NewWindowRequested += (sender, e) => e.Handled = true;
                if (address.Scheme == "http" || address.Scheme == "https")
                    webview.CoreWebView2.NavigationStarting += (sender, e) => e.Cancel = e.Uri != address.AbsoluteUri;
                webview.CoreWebView2.NavigationCompleted += (sender, e) => {
                    message.Text = e.IsSuccess ? "请手动完成验证" : "验证页加载失败：" + e.WebErrorStatus;
                    if (onNavigation != null) onNavigation(e.IsSuccess ? "navigation-ok" : e.WebErrorStatus.ToString());
                    else if (!e.IsSuccess) Program.RecordError("password", "verification_page", e.WebErrorStatus.ToString(), "HTTP=" + e.HttpStatusCode);
                };
                if (address.Scheme == "https") {
                    step = "准备官方验证回调";
                    await webview.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(@"
                        if (window.top === window && location.origin === 'https://game.bilibili.com' && location.pathname === '/sdk/geetest_v2/') {
                            window.BiliJsObject = {finishWithResult: function (result) {
                                if (typeof result === 'string' && result.length <= 4096) window.chrome.webview.postMessage(result);
                            }};
                        }");
                    if (window.IsDisposed) return;
                    webview.CoreWebView2.WebMessageReceived += async (sender, e) => {
                        if (e.Source != address.AbsoluteUri || verificationPage != address || verificationWindow != window || process == null)
                            return;
                        try {
                            string value = e.TryGetWebMessageAsString();
                            if (value.Length > 4096) return;
                            var answer = Program.Json.Deserialize<Dictionary<string, object>>(value);
                            object state;
                            if (answer == null || !answer.TryGetValue("status", out state) || !Equals(state, 1)) {
                                window.Close();
                                return;
                            }
                            verificationPage = null;
                            verify.Visible = false;
                            var proof = System.Text.Encoding.UTF8.GetBytes(value + "\n");
                            await Program.SendBackendInput(process.StandardInput.BaseStream, proof);
                        } catch (Exception ex) {
                            if (!window.IsDisposed) message.Text = "提交验证结果失败：" + ex.GetType().Name;
                            if (!cancelRequested) Program.RecordError("password", "verification_callback", ex.GetType().Name, "HRESULT=" + ex.HResult.ToString("X8"));
                        }
                    };
                }
                step = "加载验证网址";
                if (address.Scheme == "http" || address.Scheme == "https") webview.CoreWebView2.Navigate(address.AbsoluteUri);
                else webview.NavigateToString("<!doctype html><title>Verification fixture</title><p>Synthetic WebView2 test</p>");
            } catch (Exception ex) {
                if (!window.IsDisposed) message.Text = step + "失败：" + ex.GetType().Name + "（" + ex.HResult.ToString("X8") + "）";
                if (onNavigation != null) onNavigation(ex.ToString());
                else if (!cancelRequested) Program.RecordError("password", "verification_window", ex.GetType().Name, step + "。HRESULT=" + ex.HResult.ToString("X8"));
            }
        }
        byte[] RequestCredentials() {
            using (var dialog = new Form {
                Text = "B 站游戏账密登录", ClientSize = new Size(360, 166), StartPosition = FormStartPosition.CenterParent,
                FormBorderStyle = FormBorderStyle.FixedDialog, MinimizeBox = false, MaximizeBox = false, ShowInTaskbar = false
            }) {
                var account = new TextBox { Left = 95, Top = 18, Width = 245, MaxLength = 254 };
                var password = new TextBox { Left = 95, Top = 52, Width = 245, MaxLength = 200, UseSystemPasswordChar = true };
                var confirm = new Button { Text = "登录并绑定", Left = 135, Top = 118, Width = 105, Height = 29 };
                var cancel = new Button { Text = "取消", Left = 250, Top = 118, Width = 90, Height = 29, DialogResult = DialogResult.Cancel };
                dialog.Controls.AddRange(new Control[] {
                    new Label { Text = "B 站账号", Left = 18, Top = 21, Width = 72 }, account,
                    new Label { Text = "密码", Left = 18, Top = 55, Width = 72 }, password,
                    new Label { Text = "通过 B 站游戏接口登录，可能使游戏掉线。", Left = 18, Top = 88, Width = 322 },
                    confirm, cancel
                });
                dialog.AcceptButton = confirm;
                dialog.CancelButton = cancel;
                byte[] credentials = null;
                confirm.Click += (sender, e) => {
                    if (String.IsNullOrWhiteSpace(account.Text) || password.Text.Length == 0) {
                        MessageBox.Show(dialog, "请输入账号和密码。"); return;
                    }
                    credentials = System.Text.Encoding.UTF8.GetBytes(Program.Json.Serialize(new { account = account.Text.Trim(), password = password.Text }) + "\n");
                    password.Clear();
                    dialog.DialogResult = DialogResult.OK;
                };
                return dialog.ShowDialog(this) == DialogResult.OK ? credentials : null;
            }
        }
        async Task Run(bool passwordLogin) {
            if (busy) { cancelRequested = true; if (process != null && !process.HasExited) process.Kill(); return; }
            string number = qq.Text.Trim();
            if (!Regex.IsMatch(number, "^[1-9][0-9]{4,19}$")) { MessageBox.Show("请输入有效的 QQ 号。 "); return; }
            string output;
            using (var dialog = new FolderBrowserDialog { Description = "选择保存绑定文件的文件夹" }) {
                if (dialog.ShowDialog() != DialogResult.OK) return;
                output = Path.Combine(dialog.SelectedPath, number + ".fgobind");
            }
            if (File.Exists(output) && MessageBox.Show("同名绑定文件已存在，覆盖吗？", "确认", MessageBoxButtons.YesNo) != DialogResult.Yes) return;
            byte[] credentials = passwordLogin ? RequestCredentials() : null;
            if (passwordLogin && credentials == null) return;
            cancelRequested = false;
            busy = true; qq.Enabled = server.Enabled = passwordStart.Enabled = false; start.Text = "取消操作";
            status.Text = passwordLogin ? "正在登录 B 站游戏账号……" : "正在申请官方二维码……";
            string mode = passwordLogin ? "password" : "qr";
            string lastPhase = "starting";
            try {
                process = Program.Start(root, number, new[] { "android", "ios", "uo" }[server.SelectedIndex], output, false, passwordLogin);
                var errorTask = process.StandardError.ReadToEndAsync();
                if (credentials != null) {
                    // 账密通过匿名管道传给本机后端，避免出现在进程参数或日志中。
                    await Program.SendBackendInput(process.StandardInput.BaseStream, credentials);
                    Array.Clear(credentials, 0, credentials.Length);
                }
                string line; bool completed = false, reportedError = false;
                while ((line = await process.StandardOutput.ReadLineAsync()) != null) {
                    var value = Program.Json.Deserialize<Dictionary<string, object>>(line);
                    string phase = (string)value["phase"];
                    if (phase != "error") lastPhase = phase;
                    if (phase == "qr") {
                        using (var stream = new MemoryStream(Convert.FromBase64String((string)value["image"]))) {
                            var old = qr.Image; qr.Image = new Bitmap(stream); if (old != null) old.Dispose();
                        }
                        status.Text = "请在 180 秒内扫码，并在官方页面确认登录。\n登录可能使正在运行的游戏掉线。";
                    } else if (phase == "verification") {
                        verificationPage = ParseVerificationPage((string)value["url"]);
                        verify.Visible = true;
                        status.Text = "请在工具弹出的窗口中手动完成 B 站安全验证。";
                        await OpenVerificationPage();
                    } else if (phase == "requesting_cipher") status.Text = "正在向 B 站游戏 SDK 获取加密公钥……";
                    else if (phase == "submitting_password") status.Text = "正在向 B 站游戏 SDK 提交登录……";
                    else if (phase == "requesting_verification") status.Text = "B 站要求安全验证，正在获取验证会话……";
                    else if (phase == "checking_verification") {
                        verify.Visible = false;
                        verificationPage = null;
                        if (verificationWindow != null) verificationWindow.Close();
                        status.Text = "正在提交安全验证结果……";
                    } else if (phase == "waiting_confirm") status.Text = "已扫码，请在官方页面确认登录。";
                    else if (phase == "checking_account") { ClearQr(); status.Text = "正在确认所选区服角色……"; }
                    else if (phase == "complete") { completed = true; status.Text = "已生成加密绑定文件：\n" + output + "\n请私发给管理员，并告知你的 QQ 号。"; }
                    else if (phase == "error") {
                        reportedError = true;
                        var code = (string)value["code"];
                        status.Text = "操作失败：" + code;
                        object detail;
                        string explanation = value.TryGetValue("detail", out detail) ? detail as string : null;
                        if (!String.IsNullOrEmpty(explanation)) status.Text += "\n" + explanation;
                        object diagnostic;
                        string diagnostics = value.TryGetValue("diagnostic", out diagnostic) ? diagnostic as string : null;
                        if (!cancelRequested && code != "PASSWORD_VERIFICATION_CANCELLED"
                            && !Program.RecordError(mode, lastPhase, code, String.Join(" | ", explanation, diagnostics).Trim(' ', '|')))
                            status.Text += "\n无法写入 EXE 同目录的错误日志。";
                        if (code == "PASSWORD_VERIFICATION_EXPIRED")
                            status.Text += "\n安全验证已超时，请重新发起账密绑定。";
                        else if (code.StartsWith("PASSWORD_VERIFICATION_", StringComparison.Ordinal))
                            status.Text += "\n安全验证未通过，本次未生成绑定文件。";
                        else if (!passwordLogin) status.Text += "\n请检查区服或重新扫码。";
                    }
                }
                await Task.Run(() => process.WaitForExit()); await errorTask;
                if (!completed && process.ExitCode != 0 && !reportedError) {
                    status.Text = cancelRequested ? "操作已取消。" : "操作失败：后端异常退出。";
                    if (!cancelRequested) Program.RecordError(mode, lastPhase, "BACKEND_EXIT", "ExitCode=" + process.ExitCode);
                }
            } catch (Exception ex) {
                status.Text = cancelRequested ? "操作已取消。" : "操作失败：" + ex.GetType().Name;
                if (!cancelRequested) Program.RecordError(mode, lastPhase, ex.GetType().Name, "HRESULT=" + ex.HResult.ToString("X8"));
            }
            finally {
                if (credentials != null) Array.Clear(credentials, 0, credentials.Length);
                verify.Visible = false;
                verificationPage = null;
                if (verificationWindow != null) verificationWindow.Close();
                ClearQr(); if (process != null) { process.Dispose(); process = null; }
                busy = false; qq.Enabled = server.Enabled = true; start.Text = "扫码绑定";
                passwordStart.Enabled = server.SelectedIndex != 2;
            }
        }
        void ClearQr() { var old = qr.Image; qr.Image = null; if (old != null) old.Dispose(); }
    }
}
