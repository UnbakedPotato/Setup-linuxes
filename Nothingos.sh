#!/usr/bin/env bash
set -e

sudo sed -i '/\[multilib\]/,/Include/s/^#//' /etc/pacman.conf
sudo pacman -Syu --noconfirm

sudo pacman -S --needed --noconfirm \
  git rsync base-devel \
  mesa lib32-mesa libva-mesa-driver lib32-libva-mesa-driver libva-utils \
  vulkan-radeon lib32-vulkan-radeon amd-ucode \
  hyprland quickshell sddm kitty fish starship fastfetch mpv \
  firefox dolphin telegram-desktop vlc \
  easyeffects obs-studio mangohud lib32-mangohud grim slurp

if ! command -v yay &>/dev/null; then
  git clone https://aur.archlinux.org/yay.git /tmp/yay
  (cd /tmp/yay && makepkg -si --noconfirm)
fi

yay -S --needed --noconfirm ttf-nothing-font-git happ

printf 'LIBVA_DRIVER_NAME=radeonsi\nVDPAU_DRIVER=radeonsi\n' | sudo tee -a /etc/environment

sudo mkdir -p /etc/firefox/policies
sudo tee /etc/firefox/policies/policies.json >/dev/null <<'EOF'
{
  "policies": {
    "Preferences": {
      "media.ffmpeg.vaapi.enabled": true,
      "widget.dmabuf.force-enabled": true
    }
  }
}
EOF


git clone https://github.com/0xbbuddha/dotfiles_nothing_os.git ~/dotfiles_nothing_os
cd ~/dotfiles_nothing_os
./install --noconfirm

chsh -s /usr/bin/fish
