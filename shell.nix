{
  pkgs ? import <nixpkgs> { },
}:

pkgs.mkShell {
  buildInputs = [
    pkgs.yt-dlp
    pkgs.ffmpeg
    (pkgs.python3.withPackages (
      ps: with ps; [
        requests
        emoji
        mutagen
        python-dotenv
      ]
    ))
  ];
}
