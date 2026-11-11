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
        [STAThread] static int Main(string[] args) {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            try {
                string root = Prepare(args.Length == 2 && args[0] == "--self-test" ? Path.Combine(Path.GetFullPath(args[1]), "runtime") : null);
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
                if (args.Length == 2 && args[0] == "--self-test") {
                    File.WriteAllText(Path.Combine(args[1], "launcher-error.txt"), ex.ToString()); return 1;
                }
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
            // 便携版固定使用 EXE 同目录，不依赖启动环境对 AppData 的重定向或访问授权。
            string root = testRoot ?? Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ".fgo-runtime", hash.Substring(0, 20));
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
        internal static Process Start(string root, string qq, string server, string output, bool selfTest) {
            var info = new ProcessStartInfo(Path.Combine(root, "python", "python.exe"));
            info.Arguments = "-I -B -u \"" + Path.Combine(root, "backend", "local_login.py") + "\" --qq " + qq + " --server " + server + " --output \"" + output + "\"" + (selfTest ? " --self-test" : "");
            info.UseShellExecute = false; info.CreateNoWindow = true;
            info.RedirectStandardOutput = true; info.RedirectStandardError = true;
            info.StandardOutputEncoding = System.Text.Encoding.UTF8;
            info.StandardErrorEncoding = System.Text.Encoding.UTF8;
            return Process.Start(info);
        }
    }
    internal sealed class BindingForm : Form {
        readonly string root;
        readonly TextBox qq = new TextBox { Left = 120, Top = 25, Width = 260 };
        readonly ComboBox server = new ComboBox { Left = 120, Top = 65, Width = 260, DropDownStyle = ComboBoxStyle.DropDownList };
        readonly Button start = new Button { Left = 35, Top = 110, Width = 345, Height = 35, Text = "生成二维码并绑定" };
        readonly PictureBox qr = new PictureBox { Left = 55, Top = 155, Width = 310, Height = 310, SizeMode = PictureBoxSizeMode.Zoom };
        readonly Label status = new Label { Left = 25, Top = 475, Width = 375, Height = 85, Text = "填写要绑定的 QQ 号，选择游戏账号所在区服。\n生成文件后，仅私发给 FGO小助手管理员。" };
        Process process;
        bool busy;
        internal BindingForm(string directory) {
            root = directory; Text = "FGO小助手 · 本地绑定（便携版）"; ClientSize = new Size(415, 565);
            FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
            Controls.Add(new Label { Left = 30, Top = 28, Text = "绑定 QQ 号", Width = 90 });
            Controls.Add(new Label { Left = 30, Top = 68, Text = "FGO 区服", Width = 90 });
            server.Items.AddRange(new object[] { "哔哩安卓", "iOS", "享游安卓" }); server.SelectedIndex = 0;
            Controls.AddRange(new Control[] { qq, server, start, qr, status });
            start.Click += async (sender, e) => await Run();
            FormClosing += (sender, e) => { if (process != null && !process.HasExited) process.Kill(); };
        }
        async Task Run() {
            if (busy) { if (process != null && !process.HasExited) process.Kill(); return; }
            string number = qq.Text.Trim();
            if (!Regex.IsMatch(number, "^[1-9][0-9]{4,19}$")) { MessageBox.Show("请输入有效的 QQ 号。 "); return; }
            string output;
            using (var dialog = new FolderBrowserDialog { Description = "选择保存绑定文件的文件夹" }) {
                if (dialog.ShowDialog() != DialogResult.OK) return;
                output = Path.Combine(dialog.SelectedPath, number + ".fgobind");
            }
            if (File.Exists(output) && MessageBox.Show("同名绑定文件已存在，覆盖吗？", "确认", MessageBoxButtons.YesNo) != DialogResult.Yes) return;
            busy = true; qq.Enabled = server.Enabled = false; start.Text = "取消本次扫码";
            status.Text = "正在申请官方二维码……";
            try {
                process = Program.Start(root, number, new[] { "android", "ios", "uo" }[server.SelectedIndex], output, false);
                var errorTask = process.StandardError.ReadToEndAsync();
                string line; bool completed = false;
                while ((line = await process.StandardOutput.ReadLineAsync()) != null) {
                    var value = Program.Json.Deserialize<Dictionary<string, object>>(line);
                    string phase = (string)value["phase"];
                    if (phase == "qr") {
                        using (var stream = new MemoryStream(Convert.FromBase64String((string)value["image"]))) {
                            var old = qr.Image; qr.Image = new Bitmap(stream); if (old != null) old.Dispose();
                        }
                        status.Text = "请在 180 秒内扫码，并在官方页面确认登录。\n登录可能使正在运行的游戏掉线。";
                    } else if (phase == "waiting_confirm") status.Text = "已扫码，请在官方页面确认登录。";
                    else if (phase == "checking_account") { ClearQr(); status.Text = "正在确认所选区服角色……"; }
                    else if (phase == "complete") { completed = true; status.Text = "已生成加密绑定文件：\n" + output + "\n请私发给管理员，并告知你的 QQ 号。"; }
                    else if (phase == "error") status.Text = "操作失败：" + value["code"] + "\n请检查区服或重新扫码。";
                }
                await Task.Run(() => process.WaitForExit()); await errorTask;
                if (!completed && process.ExitCode != 0 && !status.Text.StartsWith("操作失败")) status.Text = "操作已取消或失败，请重新尝试。";
            } catch (Exception ex) { status.Text = "操作失败：" + ex.GetType().Name; }
            finally { ClearQr(); if (process != null) { process.Dispose(); process = null; } busy = false; qq.Enabled = server.Enabled = true; start.Text = "生成二维码并绑定"; }
        }
        void ClearQr() { var old = qr.Image; qr.Image = null; if (old != null) old.Dispose(); }
    }
}
