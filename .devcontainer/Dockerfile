FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

USER root

# Remove the broken Yarn repository before installing Dev Container features.
RUN rm -f /etc/apt/sources.list.d/yarn.list