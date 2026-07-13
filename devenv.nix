# devenv.nix
{pkgs, ...}: {
  packages = with pkgs; [
    yt-dlp
    ffmpeg

    # Python with declarative Nix packages (1:1 translation from shell.nix)
    (python3.withPackages (ps:
      with ps; [
        requests
        emoji
        mutagen
        python-dotenv
      ]))
  ];
}
