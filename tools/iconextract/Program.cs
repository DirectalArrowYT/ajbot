// Pulls the character select icons out of the game's own files into
// assets/icons/<Name>.png, so the repo never has to carry the art.
//
//   iconextract <Paks dir> <ids.json> <out dir> [aes key]
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

if (args.Length < 3)
{
    Console.Error.WriteLine("usage: iconextract <Paks dir> <ids.json> <out dir> [aes key]");
    return 2;
}
var (paks, idsPath, outDir) = (args[0], args[1], args[2]);
var key = args.Length > 3 ? args[3] : DefaultKey;
var ids = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(idsPath))!;

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

Directory.CreateDirectory(outDir);
int ok = 0, bad = 0;
foreach (var (name, id) in ids)
{
    var asset = $"{IconDir}/T_ui_ingame_VsCharSelect_CharaIcon_{id}";
    try
    {
        var tex = provider.LoadPackageObject<UTexture2D>($"{asset}.T_ui_ingame_VsCharSelect_CharaIcon_{id}");
        var img = tex.Decode() ?? throw new Exception("could not decode");
        var file = Path.Combine(outDir, name + ".png");
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
