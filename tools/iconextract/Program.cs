// Pulls the character select icons out of the game's own files into
// assets/icons/<Name>.png, so the repo never has to carry the art.
//
//   iconextract <Paks dir> <ids.json> <out dir> [aes key]
//   iconextract <Paks dir> --list <text>      print asset paths containing text
//   iconextract <Paks dir> --tex <asset path> <out.png>   one texture
//   iconextract <Paks dir> --texdir <folder> <out dir>     every texture in a folder
//   iconextract <Paks dir> --json <asset path> <out.json>  an asset's exports as JSON
//   iconextract <Paks dir> --strings <table hint> <text>   string-table entries containing text
//
// ids.json maps each roster name to its icon id ({"Bakugo": "Pl02", ...}).
// The game's IoStore container is AES-encrypted and Oodle-compressed; the
// Oodle dll is fetched next to this exe on first use.
using CUE4Parse.Compression;
using CUE4Parse.Encryption.Aes;
using CUE4Parse.FileProvider;
using CUE4Parse.UE4.Assets.Exports.Texture;
using CUE4Parse.UE4.Objects.Core.Misc;
using CUE4Parse.UE4.Versions;
using CUE4Parse_Conversion.Textures;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.PixelFormats;
using System.Text.Json;

const string DefaultKey = "0x9FF3559BC42BC5F10F0D4D3B887EC53B7CEF4E0D548C715DACE5B8204FF260D8";
const string IconDir = "HeroGame/Content/GUI/InGame/VsCharSelect/Texture/Character1P/Thumbnail";

if (args.Length < 3 && !(args.Length == 3 && args[1] == "--list"))
{
    Console.Error.WriteLine("usage: iconextract <Paks dir> <ids.json> <out dir> [aes key]");
    return 2;
}
var (paks, idsPath, outDir) = (args[0], args[1], args[2]);
var mode = idsPath.StartsWith("--") ? idsPath : "";
var key = args.Length > 3 && mode == "" ? args[3] : DefaultKey;

var oodle = Path.Combine(AppContext.BaseDirectory, OodleHelper.OODLE_NAME_CURRENT);
if (!File.Exists(oodle))
{
    Console.WriteLine("fetching the Oodle dll...");
    if (!OodleHelper.DownloadOodleDll(ref oodle))
    {
        Console.Error.WriteLine("could not download the Oodle dll");
        return 1;
    }
}
OodleHelper.Initialize(oodle);

var provider = new DefaultFileProvider(paks, SearchOption.TopDirectoryOnly,
    new VersionContainer(EGame.GAME_UE5_4), StringComparer.OrdinalIgnoreCase);
provider.Initialize();
provider.SubmitKey(new FGuid(), new FAesKey(key));
provider.PostMount();
Console.WriteLine($"{provider.Files.Count} files in the game's containers");

if (mode == "--list")
{
    foreach (var f in provider.Files.Keys.Where(f => f.Contains(outDir, StringComparison.OrdinalIgnoreCase)).Order())
        Console.WriteLine(f);
    return 0;
}
if (mode == "--json")
{
    // an asset's exports as JSON (DataTables, widget defaults), or a raw file's bytes
    var f = outDir;
    if (Path.HasExtension(f) && !f.EndsWith(".uasset"))
    {
        File.WriteAllBytes(args[3], provider.SaveAsset(f));
        return 0;
    }
    if (!f.EndsWith(".uasset")) f += ".uasset";
    var exports = provider.LoadPackage(f).GetExports();
    File.WriteAllText(args[3], Newtonsoft.Json.JsonConvert.SerializeObject(exports, Newtonsoft.Json.Formatting.Indented));
    return 0;
}
if (mode == "--strings")
{
    // every string-table entry containing <text>: "table key: value"
    foreach (var f in provider.Files.Keys.Where(f => f.Contains("/StringTable/") && f.EndsWith(".uasset")).Order())
    {
        try
        {
            var json = Newtonsoft.Json.JsonConvert.SerializeObject(provider.LoadPackage(f).GetExports());
            var entries = Newtonsoft.Json.Linq.JArray.Parse(json).SelectTokens("$..KeysToEntries").FirstOrDefault();
            if (entries is Newtonsoft.Json.Linq.JObject o)
                foreach (var p in o.Properties())
                    if (((string?)p.Value ?? "").Contains(args[3]))
                        Console.WriteLine($"{Path.GetFileNameWithoutExtension(f)} {p.Name}: {((string?)p.Value)!.Replace("\n", "\\n")}");
        }
        catch { }
    }
    return 0;
}
if (mode == "--texdir")
{
    // every texture under a folder -> <out dir>/<name>.png
    var dir = outDir.TrimEnd('/') + "/";
    Directory.CreateDirectory(args[3]);
    foreach (var f in provider.Files.Keys.Where(f => f.StartsWith(dir, StringComparison.OrdinalIgnoreCase) && f.EndsWith(".uasset")))
    {
        var n = Path.GetFileNameWithoutExtension(f);
        try
        {
            var t = provider.LoadPackageObject<UTexture2D>($"{f[..^7]}.{n}");
            Save(t.Decode() ?? throw new Exception("could not decode"), Path.Combine(args[3], n + ".png"));
        }
        catch (Exception e) { Console.Error.WriteLine($"{n}: {e.Message}"); }
    }
    return 0;
}
if (mode == "--tex")
{
    var path = outDir.Contains('.') ? outDir : $"{outDir}.{outDir.Split('/').Last()}";
    var t = provider.LoadPackageObject<UTexture2D>(path);
    var im = t.Decode() ?? throw new Exception("could not decode");
    Save(im, args[3]);
    Console.WriteLine($"{im.Width}x{im.Height} -> {args[3]}");
    return 0;
}
var ids = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(idsPath))!;

Directory.CreateDirectory(outDir);
int ok = 0, bad = 0;
foreach (var (name, id) in ids)
{
    var asset = $"{IconDir}/T_ui_ingame_VsCharSelect_CharaIcon_{id}";
    try
    {
        var tex = provider.LoadPackageObject<UTexture2D>($"{asset}.T_ui_ingame_VsCharSelect_CharaIcon_{id}");
        var img = tex.Decode() ?? throw new Exception("could not decode");
        Save(img, Path.Combine(outDir, name + ".png"));
        ok++;
    }
    catch (Exception e)
    {
        Console.Error.WriteLine($"{name} ({id}): {e.Message}");
        bad++;
    }
}
Console.WriteLine($"{ok} icons written to {outDir}" + (bad > 0 ? $", {bad} failed" : ""));
return bad > 0 && ok == 0 ? 1 : 0;

static void Save(CTexture img, string file)
{
    switch (img.PixelFormat)
    {
        case EPixelFormat.PF_B8G8R8A8:
            Image.LoadPixelData<Bgra32>(img.Data, img.Width, img.Height).SaveAsPng(file);
            break;
        case EPixelFormat.PF_R8G8B8A8:
            Image.LoadPixelData<Rgba32>(img.Data, img.Width, img.Height).SaveAsPng(file);
            break;
        default:
            throw new Exception($"decoded to {img.PixelFormat}, not 8-bit RGBA");
    }
}
