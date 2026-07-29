pipeline {
    agent { label 'wsl' }

    options {
        buildDiscarder(logRotator(numToKeepStr: '20'))
        disableConcurrentBuilds()
        timestamps()
        timeout(time: 45, unit: 'MINUTES')
    }

    parameters {
        string(name: 'MYSQL_HOST', defaultValue: '127.0.0.1', description: 'MySQL host reachable from WSL.')
        string(name: 'MYSQL_PORT', defaultValue: '3306', description: 'MySQL port.')
        string(name: 'MYSQL_USER', defaultValue: 'swt_jenkins', description: 'MySQL test user.')
        password(name: 'MYSQL_PASSWORD', defaultValue: 'swt-jenkins-local-only-2026', description: 'MySQL test password.')
        string(name: 'MYSQL_DATABASE', defaultValue: 'swt_flask_test', description: 'Disposable test database.')
    }

    environment {
        DB_BACKEND = 'mysql'
        DATABASE_URL = ''
        MYSQL_HOST = '127.0.0.1'
        MYSQL_PORT = '3306'
        MYSQL_USER = 'swt_jenkins'
        MYSQL_PASSWORD = 'swt-jenkins-local-only-2026'
        MYSQL_DATABASE = 'swt_flask_test'
        APP_SECRET_KEY = 'jenkins-dev-only-secret-key-aaaaaaaaaaaaaaaa'
        LOGIN_PASSWORD = 'jenkins-dev-admin-password'
    }

    stages {
        stage('Dev') {
            steps {
                dir('swt_flask_project') {
                    sh '''
                        python3 -m venv .jenkins-venv
                        .jenkins-venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt -r requirements-dev.txt
                        .jenkins-venv/bin/python -m pip check
                    '''
                }
            }
        }

        stage('Test') {
            steps {
                dir('swt_flask_project') {
                    sh '''
                        mkdir -p reports
                        MYSQL_HOST=127.0.0.1 \
                        MYSQL_PORT=3306 \
                        MYSQL_USER=swt_jenkins \
                        MYSQL_PASSWORD=swt-jenkins-local-only-2026 \
                        MYSQL_DATABASE=swt_flask_test \
                          .jenkins-venv/bin/python -m pytest -q --junitxml=reports/flask.xml
                    '''
                }
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'swt_flask_project/reports/*.xml'
                }
            }
        }

        stage('Build') {
            steps {
                sh '''
                    mkdir -p artifacts
                    git archive --format=zip --output="artifacts/swt-flask-final-${BUILD_NUMBER}.zip" HEAD:swt_flask_project
                '''
            }
            post {
                success {
                    archiveArtifacts artifacts: 'artifacts/swt-flask-final-*.zip', fingerprint: true
                }
            }
        }
    }

    post {
        always { deleteDir() }
    }
}
